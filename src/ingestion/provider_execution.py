"""Durable bounded acquisition shared by optional native providers.

Request reservations precede network I/O. A crashed reserved request is explicitly
indeterminate, never automatically retried (and charged again). Fresh observations
use a new request key; a completed key replays the original bytes and timestamp.
Provider credentials never enter durable request descriptions or error messages.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from src.ingestion.snapshots import SnapshotStore


class ProviderError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


_SECRET_KEYS = frozenset(
    {
        "api_key",
        "api-key",
        "apikey",
        "api_token",
        "token",
        "access_token",
        "authorization",
        "x-api-key",
        "password",
        "secret",
    }
)


def _safe_url(url, hosts, *, resolver=None):
    if not isinstance(url, str) or len(url) > 8192:
        raise ProviderError("unsafe_url", "invalid provider URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise ProviderError(
            "unsafe_url", "provider URLs must be credential-free HTTPS on port 443"
        )
    if parsed.hostname.casefold() not in hosts:
        raise ProviderError("host_forbidden", "provider URL host is not declared")
    if (
        any(k.lower() in _SECRET_KEYS for k, _ in parse_qsl(parsed.query))
        or ";jsessionid=" in url.lower()
    ):
        raise ProviderError(
            "unsafe_url", "credentials/session tokens must not occur in source URLs"
        )
    if resolver is not None:
        addresses = resolver(parsed.hostname)
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise ProviderError(
                "ssrf_blocked", "provider resolved to a non-public address"
            )
    return url


def _resolve(host):
    return sorted(
        {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    )


def _request(*, method, url, params, body, headers, timeout_s, max_bytes):
    import httpx

    # httpx's params argument replaces URL query parameters, even for {}.
    # Provider download URLs often carry required selectors such as __blob.
    request_url = httpx.URL(url).copy_merge_params(params) if params else url
    started = time.monotonic()
    # No implicit redirects, environment proxies, retries or SDK background calls:
    # each network attempt must have its own durable reservation.
    with (
        httpx.Client(
            follow_redirects=False, trust_env=False, timeout=timeout_s
        ) as client,
        client.stream(method, request_url, json=body, headers=headers) as response,
    ):
        chunks, total = [], 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ProviderError(
                    "response_limit", "provider response exceeds byte budget"
                )
            if time.monotonic() - started > timeout_s:
                raise ProviderError(
                    "deadline_exceeded", "provider response exceeded wall deadline"
                )
            chunks.append(chunk)
        return {
            "status": response.status_code,
            "headers": dict(response.headers),
            "content": b"".join(chunks),
        }


_DDL = """
CREATE TABLE IF NOT EXISTS provider_execution_budgets(
 budget_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, configuration_json TEXT NOT NULL,
 used_requests BIGINT NOT NULL, reserved_usd_micros BIGINT NOT NULL, reserved_bytes BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_execution_requests(
 budget_id TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL, state TEXT NOT NULL,
 receipt_json TEXT NOT NULL, digest TEXT, PRIMARY KEY(budget_id,request_key));
CREATE TABLE IF NOT EXISTS provider_execution_blobs(digest TEXT PRIMARY KEY, payload BLOB NOT NULL);
"""


@dataclass(frozen=True)
class CapturedResponse:
    content: bytes
    receipt: dict

    def json(self):
        try:
            return json.loads(self.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError(
                "schema_drift", "provider response is not valid JSON"
            ) from exc


class DurableHTTP:
    """One explicitly owned project budget; safe for restart and concurrent reservations.

    Use separate DuckDB connections for concurrently running clients. Bounds and
    account reference are immutable for a budget ID. Changing credentials for
    another account requires a new, non-secret account_ref and budget ID.
    """

    def __init__(
        self,
        conn,
        *,
        budget_id,
        provider,
        principal_id,
        allowed_hosts,
        reuse_notice,
        account_ref="public",
        max_requests=20,
        max_usd_micros=0,
        max_bytes=100_000_000,
        transport=None,
        resolver=None,
        now=None,
    ):
        if not all(
            isinstance(v, str) and v.strip() and len(v) <= 2000
            for v in (budget_id, provider, principal_id, reuse_notice, account_ref)
        ):
            raise ValueError(
                "explicit budget, provider, principal, account reference and reuse notice required"
            )
        if (
            any(type(v) is not int for v in (max_requests, max_usd_micros, max_bytes))
            or not 1 <= max_requests <= 1000
            or not 0 <= max_usd_micros <= 100_000_000
            or not 1 <= max_bytes <= 1_000_000_000
        ):
            raise ValueError("invalid provider budget")
        if (
            not isinstance(allowed_hosts, (list, tuple, set, frozenset))
            or not 1 <= len(allowed_hosts) <= 30
        ):
            raise ValueError("one to 30 exact provider hosts required")
        hosts = sorted({v.lower() for v in allowed_hosts})
        if any(
            not re.fullmatch(r"[a-z0-9.-]+", host)
            or ".." in host
            or host.startswith(".")
            for host in hosts
        ):
            raise ValueError("bare exact provider hostnames required")
        self.conn, self.budget_id, self.provider, self.principal_id = (
            conn,
            budget_id,
            provider,
            principal_id,
        )
        self.hosts = set(hosts)
        self.transport = transport or _request
        self.resolver = resolver or (_resolve if transport is None else None)
        self.now = now or (lambda: int(time.time() * 1000))
        self.limits = {
            "requests": max_requests,
            "usd_micros": max_usd_micros,
            "bytes": max_bytes,
        }
        self.configuration = {
            "provider": provider,
            "hosts": hosts,
            "account_ref": account_ref,
            "reuse_notice": reuse_notice,
            "limits": self.limits,
            "transport": "network" if transport is None else "injected",
        }
        conn.execute(_DDL)
        existing = conn.execute(
            "SELECT principal_id,configuration_json FROM provider_execution_budgets WHERE budget_id=?",
            [budget_id],
        ).fetchone()
        if existing and existing != (principal_id, canonical(self.configuration)):
            raise ProviderError(
                "budget_conflict", "budget owner or configuration changed"
            )
        conn.execute(
            "INSERT INTO provider_execution_budgets VALUES (?,?,?,0,0,0) ON CONFLICT DO NOTHING",
            [budget_id, principal_id, canonical(self.configuration)],
        )

    def _authorize(self, principal_id):
        if principal_id != self.principal_id:
            raise ProviderError(
                "unauthorized", "provider budget belongs to another principal"
            )

    def inspect(self, *, principal_id):
        self._authorize(principal_id)
        row = self.conn.execute(
            "SELECT used_requests,reserved_usd_micros,reserved_bytes FROM provider_execution_budgets WHERE budget_id=?",
            [self.budget_id],
        ).fetchone()
        return {
            "budget_id": self.budget_id,
            "configuration": self.configuration,
            "used_requests": row[0],
            "reserved_usd_micros": row[1],
            "reserved_bytes": row[2],
            "actual_billed_usd_micros": None,
            "cost_semantics": "conservative reservations, not billing measurements",
        }

    def request(
        self,
        request_key,
        url,
        *,
        principal_id,
        method="GET",
        params=None,
        body=None,
        headers=None,
        secret_headers=None,
        secret_params=None,
        secret_body=None,
        max_bytes=2_000_000,
        max_cost_micros=0,
        timeout_s=15,
    ):
        self._authorize(principal_id)
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 1000:
            raise ValueError("bounded explicit idempotency key required")
        if (
            method not in {"GET", "POST"}
            or not math.isfinite(timeout_s)
            or not 0.1 <= timeout_s <= 60
        ):
            raise ValueError("invalid provider request method/deadline")
        if (
            type(max_bytes) is not int
            or not 1 <= max_bytes <= 100_000_000
            or type(max_cost_micros) is not int
            or max_cost_micros < 0
        ):
            raise ValueError("invalid per-request byte/cost ceiling")
        _safe_url(url, self.hosts)
        params, headers = (
            dict(params or {}),
            {"Accept": "application/json", **dict(headers or {})},
        )
        if any(str(key).lower() in _SECRET_KEYS for key in {*params, *headers}) or any(
            str(key).lower() in _SECRET_KEYS for key in (body or {})
        ):
            raise ValueError(
                "pass credentials through secret parameters, never durable request metadata"
            )
        secret_headers, secret_params, secret_body = (
            dict(secret_headers or {}),
            dict(secret_params or {}),
            dict(secret_body or {}),
        )
        secret_values = [
            str(value)
            for values in (secret_headers, secret_params, secret_body)
            for value in values.values()
            if value
        ]
        public = {
            "url": url,
            "method": method,
            "params": params,
            "body": body,
            "headers": headers,
            "credential_slots": {
                "headers": sorted(secret_headers),
                "params": sorted(secret_params),
                "body": sorted(secret_body),
            },
            "max_bytes": max_bytes,
            "max_cost_micros": max_cost_micros,
            "timeout_s": timeout_s,
        }
        encoded = canonical(public)
        if len(encoded) > 262144 or any(
            value in encoded for value in secret_values if len(value) >= 8
        ):
            raise ValueError(
                "request metadata is excessive or contains a credential value"
            )
        request_hash = digest(public)
        # Avoid any HTTP on replay, including replay of a known failed response.
        previous = self.conn.execute(
            "SELECT request_hash,state,receipt_json,digest FROM provider_execution_requests WHERE budget_id=? AND request_key=?",
            [self.budget_id, request_key],
        ).fetchone()
        if previous:
            if previous[0] != request_hash:
                raise ProviderError(
                    "request_conflict",
                    "idempotency key is already bound to another request",
                )
            receipt = json.loads(previous[2])
            if previous[1] != "completed":
                raise ProviderError(
                    "indeterminate_request"
                    if previous[1] == "reserved"
                    else receipt.get("failure_code", "previous_failure"),
                    "previous attempt is retained; reconcile before an explicitly new attempt",
                )
            raw = self.conn.execute(
                "SELECT payload FROM provider_execution_blobs WHERE digest=?",
                [previous[3]],
            ).fetchone()
            if raw is None or hashlib.sha256(bytes(raw[0])).hexdigest() != previous[3]:
                raise ProviderError(
                    "snapshot_unavailable",
                    "captured provider payload is missing or changed",
                )
            return CapturedResponse(bytes(raw[0]), {**receipt, "replayed": True})
        # DNS/network validation is unnecessary on replay; captured bytes must
        # remain usable while offline. Resolve only for a new network attempt.
        _safe_url(url, self.hosts, resolver=self.resolver)
        receipt = {
            "contract": "noesis-provider-request-v1",
            "provider": self.provider,
            "budget_id": self.budget_id,
            "request_key": request_key,
            "request_hash": request_hash,
            "request": public,
            "reserved_cost_micros": max_cost_micros,
            "actual_billed_usd_micros": None,
            "observed_at_ms": self.now(),
            "execution": self.configuration["transport"],
            "state": "reserved",
        }
        self.conn.execute("BEGIN")
        try:
            changed = self.conn.execute(
                """UPDATE provider_execution_budgets SET used_requests=used_requests+1,
                reserved_usd_micros=reserved_usd_micros+?,reserved_bytes=reserved_bytes+?
                WHERE budget_id=? AND used_requests<? AND reserved_usd_micros+?<=? AND reserved_bytes+?<=? RETURNING budget_id""",
                [
                    max_cost_micros,
                    max_bytes,
                    self.budget_id,
                    self.limits["requests"],
                    max_cost_micros,
                    self.limits["usd_micros"],
                    max_bytes,
                    self.limits["bytes"],
                ],
            ).fetchone()
            if not changed:
                raise ProviderError(
                    "budget_exhausted",
                    "request/spend/byte reservation exceeds the explicit budget",
                )
            self.conn.execute(
                "INSERT INTO provider_execution_requests VALUES (?,?,?,'reserved',?,NULL)",
                [self.budget_id, request_key, request_hash, canonical(receipt)],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        started = time.monotonic()
        try:
            response = self.transport(
                method=method,
                url=url,
                params={**params, **secret_params},
                body={**(body or {}), **secret_body}
                if body is not None or secret_body
                else None,
                headers={**headers, **secret_headers},
                timeout_s=timeout_s,
                max_bytes=max_bytes,
            )
            raw = response.get("content", b"")
            raw = raw.encode() if isinstance(raw, str) else bytes(raw)
            if len(raw) > max_bytes:
                raise ProviderError(
                    "response_limit", "response exceeded its byte reservation"
                )
            if time.monotonic() - started > timeout_s:
                raise ProviderError(
                    "deadline_exceeded", "provider request exceeded wall deadline"
                )
            status = int(response.get("status", 200))
            response_headers = {
                str(key).lower(): str(value)
                for key, value in response.get("headers", {}).items()
            }
            receipt.update(
                http_status=status,
                retry_after=response_headers.get("retry-after"),
                elapsed_seconds=time.monotonic() - started,
            )
            if status < 200 or status >= 300:
                raise ProviderError(
                    "http_" + str(status),
                    "provider returned an unsuccessful response; no implicit redirect or retry",
                )
            if any(value.encode() in raw for value in secret_values if len(value) >= 8):
                raise ProviderError(
                    "credential_echo",
                    "provider echoed a credential; response is not safe evidence",
                )
            # Auth header schemes can be stripped by an echo. Check the token
            # portion too, without storing or logging the value.
            if any(
                value.rsplit(" ", 1)[-1].encode() in raw
                for value in secret_values
                if len(value.rsplit(" ", 1)[-1]) >= 12
            ):
                raise ProviderError(
                    "credential_echo", "provider echoed credential material"
                )
            response_digest = hashlib.sha256(raw).hexdigest()
            safe_headers = {
                key: response_headers[key]
                for key in ("content-type", "etag", "last-modified")
                if key in response_headers
            }
            if any(
                value in canonical(safe_headers)
                for value in secret_values
                if len(value) >= 8
            ):
                safe_headers = {}
            receipt.update(
                state="completed",
                digest=response_digest,
                bytes=len(raw),
                response_headers=safe_headers,
            )
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    "INSERT INTO provider_execution_blobs VALUES (?,?) ON CONFLICT DO NOTHING",
                    [response_digest, raw],
                )
                snapshot = SnapshotStore(self.conn).snapshot_bytes(
                    url,
                    raw,
                    receipt["observed_at_ms"],
                    content_type=safe_headers.get(
                        "content-type", "application/octet-stream"
                    ),
                    final_url=url,
                    max_bytes=max_bytes,
                )
                receipt["snapshot"] = snapshot
                self.conn.execute(
                    "UPDATE provider_execution_requests SET state='completed',receipt_json=?,digest=? WHERE budget_id=? AND request_key=? AND state='reserved'",
                    [canonical(receipt), response_digest, self.budget_id, request_key],
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001 - persist sanitized failure receipts for any transport error
            receipt.update(
                state="failed",
                failure_code=getattr(exc, "code", "provider_failed"),
                failure_type=type(exc).__name__,
            )
            # Error strings, HTTP URLs containing API keys, and raw failure bodies
            # are never persisted. Reservations remain conservative after failure.
            self.conn.execute(
                "UPDATE provider_execution_requests SET state='failed',receipt_json=? WHERE budget_id=? AND request_key=? AND state='reserved'",
                [canonical(receipt), self.budget_id, request_key],
            )
            raise ProviderError(
                receipt["failure_code"],
                "provider acquisition failed; inspect the credential-free receipt",
            ) from None
        return CapturedResponse(raw, receipt)
