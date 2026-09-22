"""Small first-party HTTP adapter for TypeSafe Jev.

The adapter intentionally uses the documented System One HTTP contract instead
of requiring the vendor SDK. Credentials are read only from environment
variables and are never logged or serialized into Noesis state.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
_FALSE = {"0", "false", "no", "off", "disabled"}

Transport = Callable[[str, bytes, dict[str, str], float], bytes]


class JevError(RuntimeError):
    """Safe integration error that never includes credentials."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _float_env(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if low <= value <= high else default


@dataclass(frozen=True)
class JevConfig:
    api_key: str | None
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 10.0
    noul_margin: float = 0.12
    choice_confidence: float = 0.55
    frame_margin: float = 0.08

    @classmethod
    def from_env(cls) -> JevConfig:
        enabled = os.environ.get("NOESIS_JEV_ENABLED", "true").lower() not in _FALSE
        key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("NOESIS_JEV_API_KEY")
        return cls(
            api_key=key if enabled else None,
            endpoint=os.environ.get("NOESIS_JEV_ENDPOINT", DEFAULT_ENDPOINT),
            model=os.environ.get("NOESIS_JEV_MODEL", DEFAULT_MODEL),
            timeout_seconds=_float_env("NOESIS_JEV_TIMEOUT_SECONDS", 10.0, 0.1, 120.0),
            noul_margin=_float_env("NOESIS_JEV_NOUL_MARGIN", 0.12, 0.0, 0.49),
            choice_confidence=_float_env(
                "NOESIS_JEV_CHOICE_CONFIDENCE", 0.55, 0.0, 1.0
            ),
            frame_margin=_float_env("NOESIS_JEV_FRAME_MARGIN", 0.08, 0.0, 0.49),
        )


def _http_transport(
    endpoint: str, payload: bytes, headers: dict[str, str], timeout: float
) -> bytes:
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise JevError("http_error", f"TypeSafe returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise JevError("connection_error", "TypeSafe could not be reached") from exc
    except TimeoutError as exc:
        raise JevError("timeout", "TypeSafe request timed out") from exc


class JevClient:
    """Synchronous System One client with a transport seam for tests."""

    def __init__(
        self,
        config: JevConfig | None = None,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config or JevConfig.from_env()
        self.transport = transport or _http_transport
        self.last_model = self.config.model

    @classmethod
    def configured(cls) -> bool:
        return bool(JevConfig.from_env().api_key)

    def system_one(
        self,
        *,
        state: str | Mapping[str, Any] | list[Any],
        questions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not self.config.api_key:
            raise JevError("missing_api_key", "TypeSafe API key is not configured")
        if not questions:
            raise JevError("bad_request", "at least one Jev question is required")
        payload = json.dumps(
            {
                "model": self.config.model,
                "state": state,
                "questions": dict(questions),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        raw = self.transport(
            self.config.endpoint,
            payload,
            {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Noesis/0.1 Jev",
            },
            self.config.timeout_seconds,
        )
        try:
            result = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise JevError("invalid_response", "TypeSafe returned invalid JSON") from exc
        if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
            raise JevError("invalid_response", "TypeSafe response has no answers object")
        missing = set(questions) - set(result["answers"])
        if missing:
            raise JevError(
                "invalid_response",
                f"TypeSafe response omitted {len(missing)} requested answer(s)",
            )
        model = result.get("model")
        if isinstance(model, str) and model:
            self.last_model = model
        return result


def noul_question(
    instructions: str,
    *,
    true: str | None = None,
    false: str | None = None,
) -> dict[str, Any]:
    question: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true is not None or false is not None:
        question["criteria"] = {
            "true": true or "The proposition is true.",
            "false": false or "The proposition is false.",
        }
    return question


def choice_question(
    instructions: str, criteria: Mapping[str, str]
) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": dict(criteria),
    }
