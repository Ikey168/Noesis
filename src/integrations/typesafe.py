"""Optional TypeSafe System One transport with bounded attempts and strict replies."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from datetime import timezone
from typing import Any

from src.integrations.decisions import (
    DecisionAnswer,
    DecisionError,
    DecisionReceipt,
    DecisionRequest,
    DecisionQuestion,
    parse_answers,
)


ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_RESPONSE_BYTES = 2_000_000


def _http_transport(
    *, body: dict[str, Any], api_key: str, timeout_s: float
) -> tuple[int, dict[str, str], bytes]:
    import httpx  # Installed only with the optional hosted/runtime extra.

    with httpx.Client(
        trust_env=False, follow_redirects=False, timeout=timeout_s
    ) as client:
        with client.stream(
            "POST",
            ENDPOINT,
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        ) as response:
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise DecisionError(
                        "response_limit", "provider response exceeds byte budget"
                    )
                chunks.append(chunk)
            return response.status_code, dict(response.headers), b"".join(chunks)


def _retry_after(headers: dict[str, str], now: float) -> float | None:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, target.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None


class TypeSafeClient:
    """One request per explicit attempt; retry requires caller-owned cost reservation.

    ``reserve_attempt`` is called before each network attempt with its ordinal
    and maximum microdollar allocation. A durable runtime should implement this
    callback transactionally. The default single attempt needs no callback.
    """

    def __init__(
        self,
        *,
        transport: Callable[..., tuple[int, dict[str, str], bytes]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.transport = transport or _http_transport
        self.sleep = sleep
        self.monotonic = monotonic
        self.clock = clock

    def decide(
        self,
        request: DecisionRequest,
        *,
        api_key: str,
        timeout_s: float = 20.0,
        request_id: str | None = None,
        max_attempts: int = 1,
        max_cost_micros_per_attempt: int = 0,
        reserve_attempt: Callable[[int, int], None] | None = None,
    ) -> DecisionReceipt:
        if not isinstance(request, DecisionRequest):
            raise DecisionError("invalid_input", "decide needs a DecisionRequest")
        if re.fullmatch(r"jev-[0-9]+\.[0-9]+\.[0-9]+", request.model) is None:
            raise DecisionError(
                "invalid_input", "TypeSafe requests need a pinned Jev model ID"
            )
        if not isinstance(api_key, str) or not api_key.strip():
            raise DecisionError("missing_credential", "TypeSafe credential is required")
        if (
            not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or not 0.1 <= timeout_s <= 120
        ):
            raise DecisionError("invalid_input", "timeout_s must be a bounded deadline")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise DecisionError("invalid_input", "max_attempts must be between 1 and 5")
        if (
            type(max_cost_micros_per_attempt) is not int
            or max_cost_micros_per_attempt < 0
        ):
            raise DecisionError("invalid_input", "invalid per-attempt spend allocation")
        if max_attempts > 1 and (
            reserve_attempt is None or max_cost_micros_per_attempt == 0
        ):
            raise DecisionError(
                "invalid_input",
                "retries require an explicit reservation and spend allocation",
            )
        if request_id is not None and (
            not isinstance(request_id, str) or not 1 <= len(request_id) <= 300
        ):
            raise DecisionError("invalid_input", "invalid request ID")

        deadline = self.monotonic() + float(timeout_s)
        attempts = 0
        last_code = "unavailable"
        last_status: int | None = None
        provider_request_id: str | None = None
        for ordinal in range(1, max_attempts + 1):
            remaining = deadline - self.monotonic()
            if remaining < 0.1:
                last_code = "deadline_exceeded"
                break
            if reserve_attempt is not None:
                reserve_attempt(ordinal, max_cost_micros_per_attempt)
            attempts += 1
            try:
                status, headers, content = self.transport(
                    body=request.as_wire(),
                    api_key=api_key,
                    timeout_s=remaining,
                )
            except (TimeoutError, OSError) as exc:
                last_code = (
                    "timeout" if isinstance(exc, TimeoutError) else "transport_error"
                )
                if ordinal == max_attempts:
                    break
                delay = min(2 ** (ordinal - 1), max(0, deadline - self.monotonic()))
                if delay > 0:
                    self.sleep(delay)
                continue
            except DecisionError as exc:
                last_code = exc.code
                break
            except Exception as exc:
                # httpx remains optional, so recognize its timeout class by module.
                if type(exc).__module__.startswith("httpx"):
                    last_code = (
                        "timeout"
                        if "Timeout" in type(exc).__name__
                        else "transport_error"
                    )
                    if ordinal < max_attempts:
                        self.sleep(
                            min(2 ** (ordinal - 1), max(0, deadline - self.monotonic()))
                        )
                        continue
                    break
                raise
            if (
                type(status) is not int
                or not isinstance(headers, dict)
                or not isinstance(content, bytes)
            ):
                last_code = "schema_drift"
                break
            if len(content) > MAX_RESPONSE_BYTES:
                last_code = "response_limit"
                break
            last_status = status
            provider_request_id = next(
                (
                    v
                    for k, v in headers.items()
                    if k.lower() == "x-typesafe-request-id" and isinstance(v, str)
                ),
                None,
            )
            if status == 200:
                try:
                    payload = json.loads(content)
                    model_returned, answers, usage = parse_answers(payload, request)
                except (UnicodeDecodeError, json.JSONDecodeError, DecisionError):
                    last_code = "schema_drift"
                    break
                return DecisionReceipt(
                    status="answered",
                    task_id=request.task_id,
                    rubric_id=request.rubric_id,
                    source_binding=request.source_binding,
                    model_requested=request.model,
                    model_returned=model_returned,
                    answers=answers,
                    usage=usage,
                    attempts=attempts,
                    execution={
                        "provider": "typesafe",
                        "endpoint": ENDPOINT,
                        "request_id": request_id,
                        "provider_request_id": provider_request_id,
                        "http_status": status,
                        "reserved_cost_micros": attempts * max_cost_micros_per_attempt,
                    },
                    policy_id=request.policy_id,
                    calibration_id=request.calibration_id,
                )
            if status in (401, 422):
                last_code = (
                    "authentication_failed"
                    if status == 401
                    else "invalid_provider_request"
                )
                break
            if status not in (429, 529):
                last_code = "provider_error"
                break
            last_code = "rate_limited" if status == 429 else "provider_overloaded"
            if ordinal == max_attempts:
                break
            delay = _retry_after(headers, self.clock())
            if delay is None:
                delay = min(2 ** (ordinal - 1), 8.0)
            remaining = deadline - self.monotonic()
            if delay + 0.1 > remaining:
                last_code = "deadline_exceeded"
                break
            self.sleep(delay)

        return DecisionReceipt(
            status="unavailable",
            task_id=request.task_id,
            rubric_id=request.rubric_id,
            source_binding=request.source_binding,
            model_requested=request.model,
            model_returned=None,
            answers={
                key: DecisionAnswer(
                    DecisionQuestion.from_value(value).kind, "unavailable"
                )
                for key, value in request.questions.items()
            },
            usage={"input_tokens": None, "output_tokens": None},
            attempts=attempts,
            execution={
                "provider": "typesafe",
                "endpoint": ENDPOINT,
                "request_id": request_id,
                "provider_request_id": provider_request_id,
                "http_status": last_status,
                "error_code": last_code,
                "reserved_cost_micros": attempts * max_cost_micros_per_attempt,
            },
            policy_id=request.policy_id,
            calibration_id=request.calibration_id,
        )
