"""Durable, bounded execution for installed source-pack manifests.

The runtime deliberately accepts data, not executable connector definitions.  A
small adapter factory compiles immutable source declarations into read-only page
adapters; callers may inject transports/adapters for deployment and testing.
"""

from __future__ import annotations

import email.utils
import ipaddress
import json
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from src.ingestion.document_store import DocumentStore
from src.ingestion.source_packs import (
    SUPPORTED_CONNECTORS,
    SourcePackConformance,
    SourcePackError,
    _canonical,
    _contains_secret,
    _digest,
    _load,
    _validate_endpoint,
)

REQUEST_CONTRACT = "noesis-source-pack-run-request-v1"
RECEIPT_CONTRACT = "noesis-source-pack-run-receipt-v1"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
REPLAY_CONTRACT = "noesis-source-pack-run-replay-v1"
SAFE_DETAIL_KEYS = frozenset(
    {
        "attempt",
        "classification",
        "code",
        "cursor",
        "delay_ms",
        "message",
        "page",
        "quota_remaining",
        "retry_after_ms",
        "status",
    }
)
SECRET_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "credential", "password", "secret", "token"}
)
SAFE_ERROR_CODES = frozenset(
    {
        "authentication_failed",
        "budget_exhausted",
        "document_invalid",
        "mapping_failed",
        "operation_forbidden",
        "parameter_forbidden",
        "rate_limited",
        "response_too_large",
        "schema_drift",
        "source_timeout",
        "source_unavailable",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS source_pack_license_acceptance (
  pack_id TEXT NOT NULL, source_id TEXT NOT NULL, license_id TEXT NOT NULL,
  terms_hash TEXT NOT NULL, accepted_by TEXT NOT NULL, accepted_at_ms BIGINT NOT NULL,
  redistribution BOOLEAN NOT NULL,
  PRIMARY KEY(pack_id,source_id,license_id,terms_hash,redistribution)
);
CREATE TABLE IF NOT EXISTS source_pack_runs (
  run_id TEXT PRIMARY KEY, run_key TEXT NOT NULL, request_hash TEXT NOT NULL,
  pack_id TEXT NOT NULL, pack_version TEXT NOT NULL, manifest_hash TEXT NOT NULL,
  mode TEXT NOT NULL, status TEXT NOT NULL, principal_id TEXT NOT NULL,
  request_json TEXT NOT NULL, receipt_json TEXT, started_at_ms BIGINT NOT NULL,
  updated_at_ms BIGINT NOT NULL, completed_at_ms BIGINT,
  UNIQUE(pack_id,run_key)
);
CREATE TABLE IF NOT EXISTS source_pack_source_runs (
  run_id TEXT NOT NULL, source_id TEXT NOT NULL, status TEXT NOT NULL,
  attempts BIGINT NOT NULL, pages BIGINT NOT NULL, fetched BIGINT NOT NULL,
  normalized BIGINT NOT NULL, inserted BIGINT NOT NULL, duplicates BIGINT NOT NULL,
  invalid BIGINT NOT NULL, quarantined BIGINT NOT NULL, bytes BIGINT NOT NULL,
  cursor_start TEXT, cursor_end TEXT, output_chain TEXT NOT NULL,
  failure_json TEXT, started_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(run_id,source_id)
);
CREATE TABLE IF NOT EXISTS source_pack_checkpoints (
  pack_id TEXT NOT NULL, source_id TEXT NOT NULL, pack_version TEXT NOT NULL,
  manifest_hash TEXT NOT NULL, cursor TEXT, page BIGINT NOT NULL,
  output_chain TEXT NOT NULL, committed_watermark BIGINT,
  updated_at_ms BIGINT NOT NULL, PRIMARY KEY(pack_id,source_id)
);
CREATE TABLE IF NOT EXISTS source_pack_watermarks (
  pack_id TEXT NOT NULL, watermark BIGINT NOT NULL, run_id TEXT NOT NULL UNIQUE,
  receipt_hash TEXT NOT NULL, committed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(pack_id,watermark)
);
CREATE TABLE IF NOT EXISTS source_pack_quarantine (
  quarantine_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, pack_id TEXT NOT NULL,
  source_id TEXT NOT NULL, pack_version TEXT NOT NULL, mapping_version TEXT NOT NULL,
  record_hash TEXT NOT NULL, record_json TEXT NOT NULL, error_json TEXT NOT NULL,
  state TEXT NOT NULL, attempts BIGINT NOT NULL, created_at_ms BIGINT NOT NULL,
  updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_pack_circuits (
  pack_id TEXT NOT NULL, source_id TEXT NOT NULL, state TEXT NOT NULL,
  failures BIGINT NOT NULL, opened_at_ms BIGINT, probe_after_ms BIGINT,
  last_error_json TEXT, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(pack_id,source_id)
);
CREATE TABLE IF NOT EXISTS source_pack_schedules (
  pack_id TEXT PRIMARY KEY, schedule_json TEXT NOT NULL, enabled BOOLEAN NOT NULL,
  next_run_at_ms BIGINT NOT NULL, last_run_id TEXT, updated_by TEXT NOT NULL,
  updated_at_ms BIGINT NOT NULL
);
"""


def ensure_runtime_schema(conn: Any) -> None:
    """Create the durable runtime tables for an existing Noesis database."""

    conn.execute(_DDL)


def _now() -> int:
    return int(time.time() * 1000)


def _safe_detail(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result = {
        str(key): item
        for key, item in dict(value or {}).items()
        if str(key) in SAFE_DETAIL_KEYS
    }
    if _contains_secret(result):
        raise SourcePackError(
            "embedded_secret", "runtime detail contains credential material"
        )
    return result


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]"
            if str(key).casefold() in SECRET_KEYS
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _error_summary(error: BaseException, fallback: str) -> dict[str, str]:
    candidate = str(getattr(error, "code", ""))
    code = candidate if candidate in SAFE_ERROR_CODES else fallback
    return {"code": code, "message": code.replace("_", " ")}


def _time_ms(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _validate_redirect(
    initial_url: str,
    redirect_url: str,
    resolver: Callable[[str], Sequence[str]] | None = None,
) -> None:
    """Enforce the live transport policy again for every HTTP redirect."""

    _validate_endpoint(redirect_url, "runtime-redirect")
    initial_host = (urllib.parse.urlparse(initial_url).hostname or "").casefold()
    redirect_host = (urllib.parse.urlparse(redirect_url).hostname or "").casefold()
    if redirect_host != initial_host:
        raise SourcePackError(
            "network_policy", "cross-host redirects are not permitted"
        )
    addresses = list(
        (
            resolver
            or (
                lambda host: [
                    item[4][0]
                    for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
                ]
            )
        )(redirect_host)
    )
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise SourcePackError(
            "network_policy", "redirect resolved to a non-public address"
        )


def _retry_after_ms(value: Any) -> int:
    if value is None:
        return 1_000
    try:
        return max(0, int(float(value) * 1000))
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return 1_000
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0, int(parsed.timestamp() * 1000) - _now())


def validate_run_request(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(value)
    allowed_controls = {
        "contract",
        "request_hash",
        "pack_id",
        "run_key",
        "operation",
        "mode",
        "source_ids",
        "parameters",
        "redistribute",
        "network",
        "required_sources",
        "backfill",
        "budgets",
        "max_pages",
        "max_results",
        "max_bytes",
        "timeout_ms",
        "concurrency",
        "retries",
    }
    if set(raw) - allowed_controls:
        raise SourcePackError(
            "unsupported_control", "run request contains undeclared controls"
        )
    if _contains_secret(raw):
        raise SourcePackError(
            "embedded_secret", "run requests may contain secret references, not values"
        )
    try:
        json.dumps(raw)
    except (TypeError, ValueError) as exc:
        raise SourcePackError(
            "invalid_request", "run requests must contain only JSON data"
        ) from exc
    pack_id = str(raw.get("pack_id") or "").strip()
    run_key = str(raw.get("run_key") or "").strip()
    operation = str(raw.get("operation") or "search").strip()
    mode = str(raw.get("mode") or "incremental")
    if not pack_id or not run_key or not operation:
        raise SourcePackError(
            "invalid_request", "pack_id, run_key, and operation are required"
        )
    if mode not in {"incremental", "backfill"}:
        raise SourcePackError("invalid_request", "mode must be incremental or backfill")
    source_ids = raw.get("source_ids") or []
    if isinstance(source_ids, (str, bytes)) or not isinstance(source_ids, Sequence):
        raise SourcePackError("invalid_request", "source_ids must be a list")
    source_ids = sorted({str(item) for item in source_ids if str(item)})
    budget_values = dict(raw.get("budgets") or {})
    try:
        max_pages = int(raw.get("max_pages", budget_values.get("max_pages", 10)))
        max_results = int(
            raw.get("max_results", budget_values.get("max_results", 1000))
        )
        max_bytes = int(
            raw.get("max_bytes", budget_values.get("max_bytes", 20_000_000))
        )
        timeout_ms = int(raw.get("timeout_ms", budget_values.get("timeout_ms", 60_000)))
        concurrency = int(raw.get("concurrency", budget_values.get("concurrency", 1)))
        retries = int(raw.get("retries", budget_values.get("retries", 2)))
    except (TypeError, ValueError) as exc:
        raise SourcePackError(
            "invalid_request", "runtime budgets must be integers"
        ) from exc
    if (
        not 1 <= max_pages <= 100
        or not 1 <= max_results <= 100_000
        or not 1 <= max_bytes <= 100_000_000
        or not 1 <= timeout_ms <= 120_000
        or not 1 <= concurrency <= 16
        or not 0 <= retries <= 3
    ):
        raise SourcePackError(
            "unbounded_run", "one or more runtime budgets are outside supported bounds"
        )
    backfill = dict(raw.get("backfill") or {})
    if set(backfill) - {"cursor", "from_ms", "to_ms"}:
        raise SourcePackError(
            "invalid_backfill", "backfill contains an unsupported boundary"
        )
    try:
        for key in ("from_ms", "to_ms"):
            if backfill.get(key) is not None:
                backfill[key] = int(backfill[key])
        if backfill.get("cursor") is not None:
            backfill["cursor"] = str(backfill["cursor"])
    except (TypeError, ValueError) as exc:
        raise SourcePackError(
            "invalid_backfill", "backfill boundaries must be valid integers"
        ) from exc
    if mode == "backfill" and not any(
        backfill.get(key) is not None for key in ("cursor", "from_ms", "to_ms")
    ):
        raise SourcePackError(
            "unbounded_backfill", "backfill requires a cursor or time boundary"
        )
    if (
        backfill.get("from_ms") is not None
        and backfill.get("to_ms") is not None
        and int(backfill["from_ms"]) >= int(backfill["to_ms"])
    ):
        raise SourcePackError("invalid_backfill", "backfill from_ms must precede to_ms")
    normalized = {
        "contract": REQUEST_CONTRACT,
        "pack_id": pack_id,
        "run_key": run_key,
        "operation": operation,
        "mode": mode,
        "source_ids": source_ids,
        "parameters": dict(raw.get("parameters") or {}),
        "redistribute": bool(raw.get("redistribute", False)),
        "network": str(raw.get("network") or "disabled"),
        "required_sources": sorted(
            {str(item) for item in raw.get("required_sources") or []}
        ),
        "backfill": {
            key: backfill.get(key)
            for key in ("cursor", "from_ms", "to_ms")
            if backfill.get(key) is not None
        },
        "budgets": {
            "max_pages": max_pages,
            "max_results": max_results,
            "max_bytes": max_bytes,
            "timeout_ms": timeout_ms,
            "concurrency": concurrency,
            "retries": retries,
        },
    }
    if normalized["network"] not in {"disabled", "live"}:
        raise SourcePackError("invalid_request", "network must be disabled or live")
    normalized["request_hash"] = _digest(normalized)
    return normalized


@dataclass(frozen=True)
class RuntimePage:
    records: tuple[Mapping[str, Any], ...]
    next_cursor: str | None = None
    bytes_read: int = 0
    retry_after_ms: int | None = None
    receipt: Mapping[str, Any] | None = None


@runtime_checkable
class RuntimeSourceAdapter(Protocol):
    def describe(self) -> dict[str, Any]: ...
    def fetch_page(
        self, request: Mapping[str, Any], *, cursor: str | None
    ) -> RuntimePage: ...


class HTTPSPageAdapter:
    """A fixed-GET adapter; transport remains injectable for every live deployment."""

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Any] | None = None,
        secret: str | None = None,
    ) -> None:
        self.source = json.loads(json.dumps(source))
        self.transport = transport or self._request
        self.secret = secret
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    @staticmethod
    def _request(
        *,
        url: str,
        params: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout: float,
    ) -> Mapping[str, Any]:
        query = urllib.parse.urlencode(params)
        target = url + ("?" + query if query else "")
        request = urllib.request.Request(target, headers=dict(headers), method="GET")

        class PublicSameHostRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, response_headers, newurl):
                _validate_redirect(url, newurl)
                return super().redirect_request(
                    req, fp, code, msg, response_headers, newurl
                )

        opener = urllib.request.build_opener(PublicSameHostRedirect())
        with opener.open(request, timeout=timeout) as response:
            return {
                "status": response.status,
                "headers": dict(response.headers),
                "content": response.read(),
            }

    def fetch_page(
        self, request: Mapping[str, Any], *, cursor: str | None
    ) -> RuntimePage:
        operation = str(request.get("operation") or "")
        if operation not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        allowed = {"operation", "parameters", "limit", "from_ms", "to_ms"}
        if set(request) - allowed:
            raise SourcePackError(
                "parameter_forbidden", "runtime adapter received undeclared controls"
            )
        parameters = dict(request.get("parameters") or {})
        if cursor is not None:
            parameters["cursor"] = cursor
        parameters["limit"] = min(
            int(request.get("limit", 100)),
            int(self.definition["limits"]["max_results"]),
        )
        headers = {
            "Accept": "application/json, application/xml;q=0.8, text/plain;q=0.5"
        }
        if self.secret:
            headers["Authorization"] = "Bearer " + self.secret
        response = self.transport(
            url=self.definition["endpoint"],
            params=parameters,
            headers=headers,
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        status = int(response.get("status", 200))
        response_headers = {
            str(key).casefold(): value
            for key, value in dict(response.get("headers") or {}).items()
        }
        if status == 429:
            retry_after = response.get("retry_after_ms")
            retry_after_ms = (
                int(retry_after)
                if retry_after is not None
                else _retry_after_ms(response_headers.get("retry-after"))
            )
            error = SourcePackError(
                "rate_limited",
                "source quota is temporarily exhausted",
                retry_after_ms=retry_after_ms,
            )
            raise error
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", "source rejected configured authentication"
            )
        if status < 200 or status >= 300:
            raise SourcePackError(
                "source_unavailable", f"source returned HTTP {status}"
            )
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "source response exceeds its byte limit"
            )
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {
                "records": [
                    {
                        "content": raw.decode(errors="replace"),
                        "content_type": response.get("content_type"),
                    }
                ]
            }
        if isinstance(payload, list):
            records = payload
            next_cursor = None
        elif isinstance(payload, Mapping):
            records = next(
                (
                    payload.get(key)
                    for key in ("records", "items", "results", "data")
                    if isinstance(payload.get(key), list)
                ),
                [payload],
            )
            next_cursor = payload.get("next_cursor") or payload.get("next")
        else:
            raise SourcePackError(
                "schema_drift", "source response has no record collection"
            )
        clean = tuple(dict(item) for item in records if isinstance(item, Mapping))
        return RuntimePage(
            clean,
            str(next_cursor) if next_cursor is not None else None,
            len(raw),
            receipt={
                "status": status,
                "quota_remaining": response.get("quota_remaining")
                or response_headers.get("ratelimit-remaining")
                or response_headers.get("x-ratelimit-remaining"),
            },
        )


class FixturePageAdapter:
    """Deterministic paged adapter for conformance, replay, and failure injection."""

    def __init__(
        self,
        source: Mapping[str, Any],
        pages: Sequence[Sequence[Mapping[str, Any]]],
        *,
        failures: Mapping[int, BaseException] | None = None,
    ) -> None:
        self.source = dict(source)
        self.pages = [[dict(item) for item in page] for page in pages]
        self.failures = dict(failures or {})
        self.calls: list[str | None] = []
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def fetch_page(
        self, request: Mapping[str, Any], *, cursor: str | None
    ) -> RuntimePage:
        self.calls.append(cursor)
        index = int(cursor or 0)
        if index in self.failures:
            error = self.failures.pop(index)
            raise error
        records = self.pages[index] if index < len(self.pages) else []
        next_cursor = str(index + 1) if index + 1 < len(self.pages) else None
        return RuntimePage(
            tuple(records),
            next_cursor,
            len(_canonical(records).encode()),
            receipt={"status": 200},
        )


class RuntimeAdapterFactory:
    def __init__(
        self, builders: Mapping[str, Callable[..., RuntimeSourceAdapter]] | None = None
    ) -> None:
        self.builders = {kind: HTTPSPageAdapter for kind in SUPPORTED_CONNECTORS}
        self.builders.update(dict(builders or {}))

    def compile(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Any] | None = None,
        secret: str | None = None,
    ) -> RuntimeSourceAdapter:
        kind = str(source.get("connector") or "")
        if kind not in SUPPORTED_CONNECTORS or kind not in self.builders:
            raise SourcePackError(
                "unknown_connector", "runtime connector is not allowlisted"
            )
        clean = json.loads(json.dumps(source))
        claimed = clean.pop("source_hash", None)
        if claimed != _digest(clean):
            raise SourcePackError(
                "manifest_drift", "source declaration changed after installation"
            )
        builder = self.builders[kind]
        if builder is HTTPSPageAdapter:
            return builder(source, transport=transport, secret=secret)
        return builder(source)


class SourcePackRuntime:
    def __init__(
        self,
        conn: Any,
        *,
        factory: RuntimeAdapterFactory | None = None,
        document_store: DocumentStore | None = None,
        now: Callable[[], int] = _now,
        sleep: Callable[[float], None] = time.sleep,
        initialize: bool = True,
    ) -> None:
        self.conn, self.factory, self.now, self.sleep = (
            conn,
            factory or RuntimeAdapterFactory(),
            now,
            sleep,
        )
        if initialize:
            ensure_runtime_schema(conn)
        self.documents = document_store or (DocumentStore(conn) if initialize else None)
        self._cancel: dict[str, threading.Event] = {}

    def _manifest(self, pack_id: str) -> tuple[dict[str, Any], bool]:
        row = self.conn.execute(
            "SELECT c.enabled,v.manifest_json FROM source_pack_current c JOIN source_pack_versions v ON v.pack_id=c.pack_id AND v.version=c.version WHERE c.pack_id=?",
            [pack_id],
        ).fetchone()
        if not row:
            raise SourcePackError("not_found", "source pack is not installed")
        return _load(row[1], {}), bool(row[0])

    def accept_license(
        self,
        pack_id: str,
        source_id: str,
        *,
        principal_id: str,
        redistribution: bool = False,
        accepted_at_ms: int | None = None,
    ) -> dict[str, Any]:
        manifest, _ = self._manifest(pack_id)
        source = next(
            (item for item in manifest["sources"] if item["source_id"] == source_id),
            None,
        )
        if source is None:
            raise SourcePackError("not_found", "source does not belong to the pack")
        license_policy = source["license"]
        terms_hash = _digest(
            {
                "terms_url": license_policy["terms_url"],
                "redistribution": license_policy["redistribution"],
            }
        )
        now = accepted_at_ms or self.now()
        self.conn.execute(
            "INSERT OR REPLACE INTO source_pack_license_acceptance VALUES (?,?,?,?,?,?,?)",
            [
                pack_id,
                source_id,
                license_policy["id"],
                terms_hash,
                principal_id,
                now,
                bool(redistribution),
            ],
        )
        return {
            "pack_id": pack_id,
            "source_id": source_id,
            "license_id": license_policy["id"],
            "terms_hash": terms_hash,
            "redistribution": bool(redistribution),
            "accepted_by": principal_id,
            "accepted_at_ms": now,
        }

    def preflight(
        self,
        request: Mapping[str, Any],
        *,
        secret_available: Callable[[str], bool] | None = None,
        dns_resolver: Callable[[str], Sequence[str]] | None = None,
    ) -> dict[str, Any]:
        normalized = validate_run_request(request)
        manifest, enabled = self._manifest(normalized["pack_id"])
        selected = [
            source
            for source in manifest["sources"]
            if not normalized["source_ids"]
            or source["source_id"] in normalized["source_ids"]
        ]
        unknown = set(normalized["source_ids"]) - {
            source["source_id"] for source in selected
        }
        if unknown:
            raise SourcePackError(
                "not_found",
                "requested source is not in the installed pack",
                sources=sorted(unknown),
            )
        results = []
        for source in selected:
            failures = []
            try:
                _validate_endpoint(source["endpoint"], source["source_id"])
                host = urllib.parse.urlparse(source["endpoint"]).hostname or ""
                addresses = list(
                    (
                        dns_resolver
                        or (
                            lambda name: [
                                item[4][0]
                                for item in socket.getaddrinfo(
                                    name, 443, type=socket.SOCK_STREAM
                                )
                            ]
                        )
                    )(host)
                )
                if not addresses or any(
                    not ipaddress.ip_address(address).is_global for address in addresses
                ):
                    failures.append("network_policy")
            except (OSError, ValueError, SourcePackError):
                failures.append("network_policy")
            auth = source["auth"]
            secret_ready = auth["kind"] == "none" or bool(
                secret_available and secret_available(auth.get("secret_ref", ""))
            )
            if auth["kind"] == "required-secret" and not secret_ready:
                failures.append("credential_missing")
            terms_hash = _digest(
                {
                    "terms_url": source["license"]["terms_url"],
                    "redistribution": source["license"]["redistribution"],
                }
            )
            accepted = bool(
                self.conn.execute(
                    "SELECT 1 FROM source_pack_license_acceptance WHERE pack_id=? AND source_id=? AND license_id=? AND terms_hash=? AND redistribution=?",
                    [
                        manifest["pack_id"],
                        source["source_id"],
                        source["license"]["id"],
                        terms_hash,
                        normalized["redistribute"],
                    ],
                ).fetchone()
            )
            if not accepted:
                failures.append("license_not_accepted")
            circuit = self.conn.execute(
                "SELECT state,probe_after_ms FROM source_pack_circuits WHERE pack_id=? AND source_id=?",
                [manifest["pack_id"], source["source_id"]],
            ).fetchone()
            if circuit and circuit[0] == "open" and int(circuit[1] or 0) > self.now():
                failures.append("circuit_open")
            results.append(
                {
                    "source_id": source["source_id"],
                    "ready": not failures,
                    "failures": sorted(set(failures)),
                    "authentication": {
                        "kind": auth["kind"],
                        "secret_ref": auth.get("secret_ref"),
                        "available": secret_ready,
                    },
                    "license": {
                        "id": source["license"]["id"],
                        "terms_hash": terms_hash,
                        "accepted": accepted,
                    },
                    "network": {
                        "scheme": "https",
                        "host": urllib.parse.urlparse(source["endpoint"]).hostname,
                    },
                }
            )
        return {
            "contract": "noesis-source-pack-preflight-v1",
            "pack_id": manifest["pack_id"],
            "version": manifest["version"],
            "manifest_hash": manifest["manifest_hash"],
            "enabled": enabled,
            "ready": enabled
            and bool(results)
            and all(item["ready"] for item in results),
            "sources": results,
            "request_hash": normalized["request_hash"],
            "run_id": "source-run:"
            + _digest([manifest["pack_id"], normalized["run_key"]])[:24],
        }

    def fixture_adapters(
        self, pack_id: str, root: Any
    ) -> dict[str, RuntimeSourceAdapter]:
        """Compile pinned pack fixtures into deterministic, network-free adapters."""

        manifest, _ = self._manifest(pack_id)
        conformance = SourcePackConformance(root)
        result = {}
        for source in manifest["sources"]:
            fixture = conformance._fixture(source)  # validated path and content hash
            result[source["source_id"]] = FixturePageAdapter(
                source, [list(fixture.get("normalized") or [])]
            )
        return result

    def _normalize(
        self,
        manifest: Mapping[str, Any],
        source: Mapping[str, Any],
        record: Mapping[str, Any],
        *,
        run_id: str,
        observed_at_ms: int,
    ) -> dict[str, Any]:
        record = _redact(record)
        raw_id = (
            record.get("document_id")
            or record.get("id")
            or record.get("url")
            or _digest(record)
        )
        source_type = {"paper": "paper", "blog": "blog", "web": "web"}.get(
            source["connector"], "web"
        )
        created = next(
            (
                record.get(key)
                for key in ("created_at", "published_at", "observed_at", "updated_at")
                if record.get(key) is not None
            ),
            None,
        )
        url = (
            record.get("url")
            or record.get("canonical_url")
            or (
                str(raw_id)
                if str(raw_id).startswith("https://")
                else source["endpoint"]
            )
        )
        content = record.get("content") or record.get("text") or _canonical(record)
        native_status = str(record.get("status") or "").strip().lower()
        lifecycle = (
            "deleted"
            if record.get("deleted") is True
            or record.get("_deleted") is True
            or native_status in {"deleted", "removed", "tombstone"}
            else "retracted"
            if record.get("retracted") is True
            or native_status in {"retracted", "withdrawn"}
            else "active"
        )
        return {
            # Source identity must remain stable when the record changes; the
            # immutable revision layer owns content identity.
            "document_id": "spdoc:" + _digest([source["source_id"], str(raw_id)])[:28],
            "source_type": source_type,
            "language": str(record.get("language") or "en"),
            "ingested_at": observed_at_ms,
            "created_at": _time_ms(created, observed_at_ms)
            if created is not None
            else None,
            "source_id": source["source_id"],
            "url": str(url),
            "title": str(
                record.get("title")
                or record.get("name")
                or f"{source['publisher']} record {raw_id}"
            ),
            "content": str(content),
            "authors": list(record.get("authors") or []),
            "metadata": {
                "source_pack_id": manifest["pack_id"],
                "source_pack_version": manifest["version"],
                "source_pack_manifest_hash": manifest["manifest_hash"],
                "source_pack_run_id": run_id,
                "source_pack_source_hash": source["source_hash"],
                "source_pack_mapping_schema": source["mapping"]["target_schema"],
                "source_pack_mapping_version": source["mapping"]["version"],
                "source_pack_extractor_versions": source["extractor_versions"],
                "source_pack_record_id": str(raw_id),
                "source_pack_temporal_json": _canonical(
                    {
                        key: record.get(key)
                        for key in (
                            "published_at",
                            "updated_at",
                            "observed_at",
                            "effective_at",
                            "vintage",
                            "version",
                            "status",
                        )
                        if record.get(key) is not None
                    }
                ),
                "source_pack_native_json": _canonical(record),
                "lifecycle": lifecycle,
                "tombstone": lifecycle == "deleted",
                **(
                    {"valid_to_ms": _time_ms(record["valid_to"], observed_at_ms)}
                    if record.get("valid_to") is not None
                    else {}
                ),
            },
        }

    def _quarantine(
        self,
        run_id: str,
        manifest: Mapping[str, Any],
        source: Mapping[str, Any],
        record: Mapping[str, Any],
        error: BaseException,
        now: int,
    ) -> str:
        record = _redact(record)
        record_hash = _digest(record)
        qid = (
            "source-quarantine:"
            + _digest([run_id, source["source_id"], record_hash])[:24]
        )
        safe_error = _error_summary(error, "mapping_failed")
        self.conn.execute(
            "INSERT OR IGNORE INTO source_pack_quarantine VALUES (?,?,?,?,?,?,?,?,?,'pending',0,?,?)",
            [
                qid,
                run_id,
                manifest["pack_id"],
                source["source_id"],
                manifest["version"],
                source["mapping"]["version"],
                record_hash,
                _canonical(record),
                _canonical(safe_error),
                now,
                now,
            ],
        )
        return qid

    def _circuit(
        self, pack_id: str, source_id: str, error: BaseException | None, now: int
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT failures FROM source_pack_circuits WHERE pack_id=? AND source_id=?",
            [pack_id, source_id],
        ).fetchone()
        failures = 0 if error is None else int(row[0] if row else 0) + 1
        state = "closed" if error is None else "open" if failures >= 3 else "closed"
        probe = (
            now + min(300_000, 1000 * 2 ** min(failures, 8))
            if state == "open"
            else None
        )
        detail = (
            None
            if error is None
            else _canonical(_error_summary(error, "source_failed"))
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO source_pack_circuits VALUES (?,?,?,?,?,?,?,?)",
            [
                pack_id,
                source_id,
                state,
                failures,
                now if state == "open" else None,
                probe,
                detail,
                now,
            ],
        )
        return {"state": state, "failures": failures, "probe_after_ms": probe}

    def cancel(self, run_id: str) -> dict[str, Any]:
        event = self._cancel.get(run_id)
        if event is None:
            row = self.conn.execute(
                "SELECT status FROM source_pack_runs WHERE run_id=?", [run_id]
            ).fetchone()
            return {
                "run_id": run_id,
                "cancelled": False,
                "status": row[0] if row else "not_found",
            }
        event.set()
        return {"run_id": run_id, "cancelled": True, "status": "cancelling"}

    def run(
        self,
        request: Mapping[str, Any],
        *,
        principal_id: str,
        adapters: Mapping[str, RuntimeSourceAdapter] | None = None,
        transports: Mapping[str, Callable[..., Any]] | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
        dns_resolver: Callable[[str], Sequence[str]] | None = None,
        fault: Callable[[str, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        advance_schedule: bool = True,
    ) -> dict[str, Any]:
        if self.documents is None:
            raise SourcePackError(
                "runtime_read_only",
                "execution requires an initialized writable runtime",
            )
        normalized = validate_run_request(request)
        manifest, enabled = self._manifest(normalized["pack_id"])
        if not enabled:
            raise SourcePackError("pack_disabled", "source pack is disabled")
        availability = lambda ref: bool(secret_resolver and secret_resolver(ref))
        preflight = self.preflight(
            normalized, secret_available=availability, dns_resolver=dns_resolver
        )
        selected = [
            source
            for source in manifest["sources"]
            if not normalized["source_ids"]
            or source["source_id"] in normalized["source_ids"]
        ]
        required = set(normalized["required_sources"]) or {
            source["source_id"]
            for source in selected
            if dict(source.get("health") or {}).get("required", False)
        }
        unknown_required = required - {source["source_id"] for source in selected}
        if unknown_required:
            raise SourcePackError(
                "invalid_request",
                "required_sources must be included in the selected sources",
                sources=sorted(unknown_required),
            )
        blocked_required = required & {
            item["source_id"] for item in preflight["sources"] if not item["ready"]
        }
        if blocked_required:
            raise SourcePackError(
                "preflight_failed",
                "required source failed runtime preflight",
                sources=sorted(blocked_required),
            )
        run_id = (
            "source-run:" + _digest([manifest["pack_id"], normalized["run_key"]])[:24]
        )
        now = self.now()
        prior = self.conn.execute(
            "SELECT request_hash,status,receipt_json,pack_version,manifest_hash FROM source_pack_runs WHERE pack_id=? AND run_key=?",
            [manifest["pack_id"], normalized["run_key"]],
        ).fetchone()
        if prior:
            if prior[0] != normalized["request_hash"]:
                raise SourcePackError(
                    "idempotency_conflict", "run key was reused with different controls"
                )
            if prior[3] != manifest["version"] or prior[4] != manifest["manifest_hash"]:
                raise SourcePackError(
                    "manifest_drift", "installed pack changed before run resume"
                )
            if prior[1] in {"complete", "partial"}:
                result = _load(prior[2], {})
                result["idempotent"] = True
                return result
        else:
            active = self.conn.execute(
                "SELECT run_id FROM source_pack_runs WHERE pack_id=? AND status='running'",
                [manifest["pack_id"]],
            ).fetchone()
            if active:
                raise SourcePackError(
                    "run_conflict",
                    "another run is active for this pack",
                    run_id=active[0],
                )
            self.conn.execute(
                "INSERT INTO source_pack_runs VALUES (?,?,?,?,?,?,?,'running',?,?,NULL,?,?,NULL)",
                [
                    run_id,
                    normalized["run_key"],
                    normalized["request_hash"],
                    manifest["pack_id"],
                    manifest["version"],
                    manifest["manifest_hash"],
                    normalized["mode"],
                    principal_id,
                    _canonical(normalized),
                    now,
                    now,
                ],
            )
        self.conn.execute(
            "UPDATE source_pack_runs SET status='running',updated_at_ms=? WHERE run_id=?",
            [now, run_id],
        )
        event = self._cancel.setdefault(run_id, threading.Event())
        started = time.monotonic()
        source_receipts = []
        failures = []
        try:
            for source in selected:
                readiness = next(
                    item
                    for item in preflight["sources"]
                    if item["source_id"] == source["source_id"]
                )
                if not readiness["ready"]:
                    failures.append(
                        {
                            "source_id": source["source_id"],
                            "classification": "preflight",
                            "detail": readiness["failures"],
                        }
                    )
                    continue
                checkpoint = self.conn.execute(
                    "SELECT pack_version,manifest_hash,cursor,page,output_chain FROM source_pack_checkpoints WHERE pack_id=? AND source_id=?",
                    [manifest["pack_id"], source["source_id"]],
                ).fetchone()
                if checkpoint and (
                    checkpoint[0] != manifest["version"]
                    or checkpoint[1] != manifest["manifest_hash"]
                ):
                    raise SourcePackError(
                        "cursor_drift",
                        "checkpoint belongs to a different pack generation",
                        source_id=source["source_id"],
                    )
                existing = self.conn.execute(
                    "SELECT attempts,pages,fetched,normalized,inserted,duplicates,invalid,quarantined,bytes,cursor_start,cursor_end,output_chain FROM source_pack_source_runs WHERE run_id=? AND source_id=?",
                    [run_id, source["source_id"]],
                ).fetchone()
                cursor = (
                    str(normalized["backfill"].get("cursor"))
                    if normalized["mode"] == "backfill"
                    and normalized["backfill"].get("cursor") is not None
                    else (
                        checkpoint[2]
                        if checkpoint and normalized["mode"] == "incremental"
                        else None
                    )
                )
                counts = {
                    "attempts": 0,
                    "pages": 0,
                    "fetched": 0,
                    "normalized": 0,
                    "inserted": 0,
                    "duplicates": 0,
                    "invalid": 0,
                    "quarantined": 0,
                    "bytes": 0,
                }
                retries: list[dict[str, Any]] = []
                chain = _digest([])
                cursor_start = cursor
                if existing:
                    for index, key in enumerate(counts):
                        counts[key] = int(existing[index])
                        cursor_start = existing[9]
                        cursor = existing[10]
                        chain = existing[11]
                else:
                    self.conn.execute(
                        "INSERT INTO source_pack_source_runs VALUES (?,?,'running',0,0,0,0,0,0,0,0,0,?,?,?,NULL,?,?)",
                        [
                            run_id,
                            source["source_id"],
                            cursor_start,
                            cursor,
                            chain,
                            now,
                            now,
                        ],
                    )
                secret_ref = source["auth"].get("secret_ref")
                secret = (
                    secret_resolver(secret_ref)
                    if secret_ref and secret_resolver
                    else None
                )
                adapter = (adapters or {}).get(source["source_id"])
                if adapter is None and normalized["network"] != "live":
                    raise SourcePackError(
                        "network_disabled",
                        "live source execution requires network=live or a fixture adapter",
                    )
                adapter = adapter or self.factory.compile(
                    source,
                    transport=(transports or {}).get(source["source_id"]),
                    secret=secret,
                )
                description = adapter.describe()
                if description.get("source_hash") != source["source_hash"]:
                    raise SourcePackError(
                        "manifest_drift",
                        "runtime adapter does not match installed source",
                    )
                source_error = None
                while counts["pages"] < min(
                    normalized["budgets"]["max_pages"], source["budgets"]["max_pages"]
                ):
                    if event.is_set() or cancelled and cancelled():
                        raise SourcePackError(
                            "cancelled", "source-pack run was cancelled"
                        )
                    if (time.monotonic() - started) * 1000 >= normalized["budgets"][
                        "timeout_ms"
                    ]:
                        raise SourcePackError(
                            "deadline_exceeded", "source-pack run exceeded its deadline"
                        )
                    page = None
                    for attempt in range(normalized["budgets"]["retries"] + 1):
                        counts["attempts"] += 1
                        try:
                            page = adapter.fetch_page(
                                {
                                    "operation": normalized["operation"],
                                    "parameters": normalized["parameters"],
                                    "limit": min(
                                        normalized["budgets"]["max_results"]
                                        - counts["fetched"],
                                        source["budgets"]["max_results"],
                                    ),
                                    **{
                                        key: value
                                        for key, value in normalized["backfill"].items()
                                        if key != "cursor"
                                    },
                                },
                                cursor=cursor,
                            )
                            source_error = None
                            break
                        except Exception as exc:  # noqa: BLE001 - adapter isolation boundary
                            source_error = exc
                            retryable = getattr(exc, "code", "") in {
                                "rate_limited",
                                "source_timeout",
                                "source_unavailable",
                            }
                            if (
                                not retryable
                                or attempt >= normalized["budgets"]["retries"]
                            ):
                                break
                            delay = min(
                                30_000,
                                int(
                                    getattr(exc, "details", {}).get(
                                        "retry_after_ms", 100 * 2**attempt
                                    )
                                ),
                            )
                            retries.append(
                                {
                                    "attempt": attempt + 1,
                                    "code": _error_summary(exc, "source_failed")[
                                        "code"
                                    ],
                                    "delay_ms": delay,
                                }
                            )
                            self.sleep(delay / 1000)
                    if page is None:
                        break
                    counts["pages"] += 1
                    counts["fetched"] += len(page.records)
                    counts["bytes"] += page.bytes_read
                    if counts["fetched"] > normalized["budgets"][
                        "max_results"
                    ] or counts["bytes"] > min(
                        normalized["budgets"]["max_bytes"],
                        source["budgets"]["max_bytes"],
                    ):
                        source_error = SourcePackError(
                            "budget_exhausted", "source exceeded result or byte budget"
                        )
                        break
                    documents = []
                    for record in page.records:
                        try:
                            documents.append(
                                self._normalize(
                                    manifest,
                                    source,
                                    record,
                                    run_id=run_id,
                                    observed_at_ms=self.now(),
                                )
                            )
                            counts["normalized"] += 1
                        except Exception as exc:  # noqa: BLE001 - quarantine one bad record
                            self._quarantine(
                                run_id, manifest, source, record, exc, self.now()
                            )
                            counts["invalid"] += 1
                            counts["quarantined"] += 1
                    for document in documents:
                        outcome = self.documents.upsert([document])
                        counts["inserted"] += outcome.inserted
                        counts["duplicates"] += outcome.duplicate
                        counts["invalid"] += outcome.invalid
                        if outcome.invalid:
                            self._quarantine(
                                run_id,
                                manifest,
                                source,
                                document,
                                SourcePackError(
                                    "document_invalid", outcome.dead_letter[0]["error"]
                                ),
                                self.now(),
                            )
                            counts["quarantined"] += 1
                    chain = _digest(
                        [chain, [_digest(record) for record in page.records]]
                    )
                    cursor = page.next_cursor
                    updated = self.now()
                    self.conn.execute(
                        "UPDATE source_pack_source_runs SET attempts=?,pages=?,fetched=?,normalized=?,inserted=?,duplicates=?,invalid=?,quarantined=?,bytes=?,cursor_end=?,output_chain=?,updated_at_ms=? WHERE run_id=? AND source_id=?",
                        [
                            *[counts[key] for key in counts],
                            cursor,
                            chain,
                            updated,
                            run_id,
                            source["source_id"],
                        ],
                    )
                    if normalized["mode"] == "incremental":
                        self.conn.execute(
                            "INSERT OR REPLACE INTO source_pack_checkpoints VALUES (?,?,?,?,?,?,?,NULL,?)",
                            [
                                manifest["pack_id"],
                                source["source_id"],
                                manifest["version"],
                                manifest["manifest_hash"],
                                cursor,
                                counts["pages"],
                                chain,
                                updated,
                            ],
                        )
                    if fault:
                        fault(source["source_id"], counts["pages"])
                    if cursor is None:
                        break
                status = "failed" if source_error else "complete"
                failure = (
                    None
                    if source_error is None
                    else {
                        **_error_summary(source_error, "source_failed"),
                        "classification": SourcePackConformance.classify(source_error),
                    }
                )
                self.conn.execute(
                    "UPDATE source_pack_source_runs SET status=?,failure_json=?,updated_at_ms=? WHERE run_id=? AND source_id=?",
                    [
                        status,
                        None if failure is None else _canonical(failure),
                        self.now(),
                        run_id,
                        source["source_id"],
                    ],
                )
                circuit = self._circuit(
                    manifest["pack_id"], source["source_id"], source_error, self.now()
                )
                receipt = {
                    "source_id": source["source_id"],
                    "status": status,
                    "required": source["source_id"] in required,
                    "counts": counts,
                    "cursor": {
                        "start": cursor_start,
                        "end": cursor,
                        "live_advanced": normalized["mode"] == "incremental",
                    },
                    "output_hash": chain,
                    "failure": failure,
                    "retries": retries,
                    "circuit": circuit,
                    "adapter": description,
                }
                source_receipts.append(receipt)
                if source_error:
                    failures.append(
                        {
                            "source_id": source["source_id"],
                            "classification": failure["classification"],
                            "error": {
                                "code": failure["code"],
                                "message": failure["message"],
                            },
                        }
                    )
            required_failed = required & {item["source_id"] for item in failures}
            status = (
                "failed" if required_failed else "partial" if failures else "complete"
            )
            watermark = None
            stable = {
                "run_id": run_id,
                "request_hash": normalized["request_hash"],
                "pack_id": manifest["pack_id"],
                "pack_version": manifest["version"],
                "manifest_hash": manifest["manifest_hash"],
                "mode": normalized["mode"],
                "status": status,
                "sources": source_receipts,
                "failures": failures,
            }
            receipt_hash = ""
            change_set = None
            transaction_open = False
            if not required_failed:
                prior_mark = self.conn.execute(
                    "SELECT watermark FROM source_pack_watermarks WHERE pack_id=? ORDER BY watermark DESC LIMIT 1",
                    [manifest["pack_id"]],
                ).fetchone()
                watermark = int(prior_mark[0] if prior_mark else 0) + 1
                committed = self.now()
                # The watermark, change set, and final run receipt form one
                # visibility boundary.  Maintenance never sees a partial one.
                self.conn.execute("BEGIN")
                transaction_open = True
                from src.ingestion.revisions import DocumentRevisionStore

                change_set = DocumentRevisionStore(
                    self.conn, initialize=False
                ).commit_change_set(
                    manifest["pack_id"],
                    watermark,
                    run_id,
                    committed_at_ms=committed,
                )
                stable["change_set"] = change_set
                receipt_hash = _digest(stable)
                self.conn.execute(
                    "INSERT INTO source_pack_watermarks VALUES (?,?,?,?,?)",
                    [manifest["pack_id"], watermark, run_id, receipt_hash, committed],
                )
                if normalized["mode"] == "incremental":
                    self.conn.execute(
                        "UPDATE source_pack_checkpoints SET committed_watermark=? WHERE pack_id=?",
                        [watermark, manifest["pack_id"]],
                    )
            else:
                receipt_hash = _digest(stable)
            receipt = {
                "contract": RECEIPT_CONTRACT,
                **stable,
                "receipt_hash": receipt_hash,
                "watermark": watermark,
                "coverage": {
                    "configured": len(manifest["sources"]),
                    "selected": len(selected),
                    "attempted": len(source_receipts),
                    "completed": sum(
                        item["status"] == "complete" for item in source_receipts
                    ),
                    "quarantined": sum(
                        item["counts"]["quarantined"] for item in source_receipts
                    ),
                    "degraded": len(failures),
                    "unavailable": len(
                        [item for item in preflight["sources"] if not item["ready"]]
                    ),
                },
                "budgets": normalized["budgets"],
                "timing": {
                    "started_at_ms": now,
                    "completed_at_ms": self.now(),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                },
                "replay": {
                    "run_id": run_id,
                    "request_hash": normalized["request_hash"],
                    "manifest_hash": manifest["manifest_hash"],
                    "receipt_hash": receipt_hash,
                },
            }
            self.conn.execute(
                "UPDATE source_pack_runs SET status=?,receipt_json=?,updated_at_ms=?,completed_at_ms=? WHERE run_id=?",
                [status, _canonical(receipt), self.now(), self.now(), run_id],
            )
            if transaction_open:
                self.conn.execute("COMMIT")
                transaction_open = False
            from src.ingestion.source_packs import SourcePackStore

            health_store = SourcePackStore(self.conn, initialize=False)
            for item in source_receipts:
                health_store.record_health(
                    manifest["pack_id"],
                    item["source_id"],
                    status="healthy" if item["status"] == "complete" else "degraded",
                    classification="ok"
                    if item["failure"] is None
                    else item["failure"]["classification"],
                    detail={
                        "cursor": item["cursor"]["end"],
                        "mapping_failures": item["counts"]["quarantined"],
                        "freshness_lag_s": 0 if item["status"] == "complete" else None,
                        "last_receipt": item["output_hash"],
                        "last_watermark": watermark,
                    },
                    checked_at_ms=self.now(),
                )
            schedule = self.conn.execute(
                "SELECT schedule_json FROM source_pack_schedules WHERE pack_id=? AND enabled=true",
                [manifest["pack_id"]],
            ).fetchone()
            if schedule and advance_schedule:
                interval = int(_load(schedule[0], {})["interval_s"])
                self.conn.execute(
                    "UPDATE source_pack_schedules SET next_run_at_ms=?,last_run_id=?,updated_at_ms=? WHERE pack_id=?",
                    [
                        self.now() + interval * 1000,
                        run_id,
                        self.now(),
                        manifest["pack_id"],
                    ],
                )
            audit_id = (
                "source-pack-audit:" + _digest([run_id, status, receipt_hash])[:24]
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO source_pack_audit VALUES (?,?,?,?,?,?)",
                [
                    audit_id,
                    manifest["pack_id"],
                    principal_id,
                    "runtime-run",
                    _canonical(
                        {
                            "run_id": run_id,
                            "status": status,
                            "watermark": watermark,
                            "receipt_hash": receipt_hash,
                        }
                    ),
                    self.now(),
                ],
            )
            return receipt
        except Exception as exc:
            if locals().get("transaction_open", False):
                self.conn.execute("ROLLBACK")
            status = (
                "cancelled"
                if getattr(exc, "code", "") == "cancelled"
                else "interrupted"
            )
            self.conn.execute(
                "UPDATE source_pack_runs SET status=?,updated_at_ms=? WHERE run_id=?",
                [status, self.now(), run_id],
            )
            raise
        finally:
            self._cancel.pop(run_id, None)

    def inspect(self, run_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT pack_id,pack_version,mode,status,request_json,receipt_json,started_at_ms,updated_at_ms,completed_at_ms FROM source_pack_runs WHERE run_id=?",
            [run_id],
        ).fetchone()
        if not row:
            raise SourcePackError("not_found", "source-pack run does not exist")
        sources = []
        for value in self.conn.execute(
            "SELECT source_id,status,attempts,pages,fetched,normalized,inserted,duplicates,invalid,quarantined,bytes,cursor_start,cursor_end,output_chain,failure_json FROM source_pack_source_runs WHERE run_id=? ORDER BY source_id",
            [run_id],
        ).fetchall():
            sources.append(
                {
                    "source_id": value[0],
                    "status": value[1],
                    "counts": dict(
                        zip(
                            (
                                "attempts",
                                "pages",
                                "fetched",
                                "normalized",
                                "inserted",
                                "duplicates",
                                "invalid",
                                "quarantined",
                                "bytes",
                            ),
                            map(int, value[2:11]),
                            strict=True,
                        )
                    ),
                    "cursor": {"start": value[11], "end": value[12]},
                    "output_hash": value[13],
                    "failure": _load(value[14], None),
                }
            )
        return {
            "run_id": run_id,
            "pack_id": row[0],
            "pack_version": row[1],
            "mode": row[2],
            "status": row[3],
            "request": _load(row[4], {}),
            "receipt": _load(row[5], None),
            "started_at_ms": int(row[6]),
            "updated_at_ms": int(row[7]),
            "completed_at_ms": row[8],
            "sources": sources,
        }

    def replay(self, run_id: str) -> dict[str, Any]:
        inspected = self.inspect(run_id)
        receipt = inspected["receipt"]
        if not receipt:
            raise SourcePackError(
                "run_incomplete", "only completed runs can be replayed"
            )
        stable = {
            key: receipt[key]
            for key in (
                "run_id",
                "request_hash",
                "pack_id",
                "pack_version",
                "manifest_hash",
                "mode",
                "status",
                "sources",
                "failures",
            )
        }
        if receipt.get("change_set") is not None:
            stable["change_set"] = receipt["change_set"]
        computed = _digest(stable)
        request_matches = (
            inspected["request"].get("request_hash") == receipt["request_hash"]
        )
        stored_outputs = {
            item["source_id"]: item["output_hash"] for item in inspected["sources"]
        }
        output_matches = all(
            stored_outputs.get(item["source_id"]) == item["output_hash"]
            for item in receipt["sources"]
        )
        committed = self.conn.execute(
            "SELECT watermark,receipt_hash FROM source_pack_watermarks WHERE run_id=?",
            [run_id],
        ).fetchone()
        watermark_matches = (
            receipt["watermark"] is None
            if committed is None
            else int(committed[0]) == receipt["watermark"]
            and committed[1] == receipt["receipt_hash"]
        )
        receipt_matches = computed == receipt["receipt_hash"]
        return {
            "contract": REPLAY_CONTRACT,
            "run_id": run_id,
            "matched": receipt_matches
            and request_matches
            and output_matches
            and watermark_matches,
            "request_hash_match": request_matches,
            "output_hashes_match": output_matches,
            "watermark_hash_match": watermark_matches,
            "receipt_hash_match": receipt_matches,
            "expected_hash": receipt["receipt_hash"],
            "computed_hash": computed,
            "manifest_match": inspected["pack_version"] == receipt["pack_version"],
        }

    def retry_quarantine(
        self, quarantine_ids: Sequence[str], *, principal_id: str
    ) -> dict[str, Any]:
        if self.documents is None:
            raise SourcePackError(
                "runtime_read_only", "quarantine retry requires a writable runtime"
            )
        retried = recovered = failed = 0
        for qid in sorted(set(quarantine_ids)):
            row = self.conn.execute(
                "SELECT run_id,pack_id,source_id,pack_version,mapping_version,record_json,state,attempts FROM source_pack_quarantine WHERE quarantine_id=?",
                [qid],
            ).fetchone()
            if not row:
                raise SourcePackError(
                    "not_found", f"quarantine entry {qid} does not exist"
                )
            retried += 1
            manifest, _ = self._manifest(row[1])
            source = next(
                item for item in manifest["sources"] if item["source_id"] == row[2]
            )
            if manifest["version"] != row[3] or source["mapping"]["version"] != row[4]:
                raise SourcePackError(
                    "mapping_drift",
                    "quarantine retry requires its exact pack and mapping version",
                )
            try:
                document = self._normalize(
                    manifest,
                    source,
                    _load(row[5], {}),
                    run_id=row[0],
                    observed_at_ms=self.now(),
                )
                outcome = self.documents.upsert([document])
                state = (
                    "recovered" if outcome.inserted or outcome.duplicate else "failed"
                )
                recovered += state == "recovered"
                failed += state == "failed"
            except Exception:  # noqa: BLE001 - retry isolation boundary
                state = "failed"
                failed += 1
            self.conn.execute(
                "UPDATE source_pack_quarantine SET state=?,attempts=?,updated_at_ms=? WHERE quarantine_id=?",
                [state, int(row[7]) + 1, self.now(), qid],
            )
        return {
            "principal_id": principal_id,
            "retried": retried,
            "recovered": recovered,
            "failed": failed,
        }

    def quarantine(
        self,
        *,
        pack_id: str | None = None,
        run_id: str | None = None,
        state: str = "pending",
    ) -> dict[str, Any]:
        if state not in {"pending", "recovered", "failed", "all"}:
            raise SourcePackError("invalid_request", "unsupported quarantine state")
        clauses, params = [], []
        if pack_id:
            clauses.append("pack_id=?")
            params.append(pack_id)
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if state != "all":
            clauses.append("state=?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            "SELECT quarantine_id,run_id,pack_id,source_id,pack_version,mapping_version,"
            "record_hash,error_json,state,attempts,created_at_ms,updated_at_ms "
            f"FROM source_pack_quarantine{where} ORDER BY created_at_ms,quarantine_id LIMIT 500",
            params,
        ).fetchall()
        return {
            "contract": "noesis-source-pack-quarantine-v1",
            "items": [
                {
                    "quarantine_id": row[0],
                    "run_id": row[1],
                    "pack_id": row[2],
                    "source_id": row[3],
                    "pack_version": row[4],
                    "mapping_version": row[5],
                    "record_hash": row[6],
                    "error": _load(row[7], {}),
                    "state": row[8],
                    "attempts": int(row[9]),
                    "created_at_ms": int(row[10]),
                    "updated_at_ms": int(row[11]),
                }
                for row in rows
            ],
        }

    def set_schedule(
        self,
        pack_id: str,
        schedule: Mapping[str, Any],
        *,
        principal_id: str,
        enabled: bool = True,
    ) -> dict[str, Any]:
        self._manifest(pack_id)
        kind = str(schedule.get("kind") or "interval")
        interval = int(schedule.get("interval_s", 0))
        if kind != "interval" or not 60 <= interval <= 31_536_000:
            raise SourcePackError(
                "invalid_schedule", "only bounded interval schedules are supported"
            )
        now = self.now()
        next_run = int(schedule.get("next_run_at_ms") or now + interval * 1000)
        safe = {"kind": kind, "interval_s": interval}
        self.conn.execute(
            "INSERT OR REPLACE INTO source_pack_schedules VALUES (?,?,?,?,?,?,?)",
            [
                pack_id,
                _canonical(safe),
                bool(enabled),
                next_run,
                None,
                principal_id,
                now,
            ],
        )
        return {
            "pack_id": pack_id,
            "schedule": safe,
            "enabled": bool(enabled),
            "next_run_at_ms": next_run,
            "updated_by": principal_id,
        }

    def schedules(self, *, due_at_ms: int | None = None) -> dict[str, Any]:
        at = due_at_ms or self.now()
        rows = self.conn.execute(
            "SELECT pack_id,schedule_json,enabled,next_run_at_ms,last_run_id,updated_by,updated_at_ms FROM source_pack_schedules ORDER BY pack_id"
        ).fetchall()
        items = [
            {
                "pack_id": row[0],
                "schedule": _load(row[1], {}),
                "enabled": bool(row[2]),
                "next_run_at_ms": int(row[3]),
                "due": bool(row[2]) and int(row[3]) <= at,
                "last_run_id": row[4],
                "updated_by": row[5],
                "updated_at_ms": int(row[6]),
            }
            for row in rows
        ]
        return {
            "contract": "noesis-source-pack-schedules-v1",
            "at_ms": at,
            "schedules": items,
        }

    def runtime_coverage(self) -> dict[str, Any]:
        domains: dict[str, dict[str, int]] = {}
        for row in self.conn.execute(
            "SELECT v.manifest_json FROM source_pack_current c JOIN source_pack_versions v ON v.pack_id=c.pack_id AND v.version=c.version ORDER BY c.pack_id"
        ).fetchall():
            manifest = _load(row[0], {})
            mark = self.conn.execute(
                "SELECT watermark FROM source_pack_watermarks WHERE pack_id=? ORDER BY watermark DESC LIMIT 1",
                [manifest["pack_id"]],
            ).fetchone()
            for domain in manifest["domains"]:
                summary = domains.setdefault(
                    domain,
                    {
                        "configured": 0,
                        "ready": 0,
                        "attempted": 0,
                        "completed": 0,
                        "quarantined": 0,
                        "degraded": 0,
                        "unavailable": 0,
                        "watermarked_packs": 0,
                    },
                )
                summary["configured"] += len(manifest["sources"])
                summary["watermarked_packs"] += bool(mark)
                for source in manifest["sources"]:
                    health = self.conn.execute(
                        "SELECT status FROM source_pack_health WHERE pack_id=? AND source_id=?",
                        [manifest["pack_id"], source["source_id"]],
                    ).fetchone()
                    last = self.conn.execute(
                        "SELECT sr.status,sr.quarantined FROM source_pack_source_runs sr JOIN source_pack_runs r ON r.run_id=sr.run_id WHERE r.pack_id=? AND sr.source_id=? ORDER BY r.started_at_ms DESC LIMIT 1",
                        [manifest["pack_id"], source["source_id"]],
                    ).fetchone()
                    summary["ready"] += bool(
                        health and health[0] in {"healthy", "degraded"}
                    )
                    if last:
                        summary["attempted"] += 1
                        summary["completed"] += last[0] == "complete"
                        summary["quarantined"] += int(last[1])
                        summary["degraded"] += last[0] != "complete"
                        summary["unavailable"] += last[0] == "failed"
                    elif health:
                        summary["unavailable"] += health[0] == "unavailable"
        return {
            "contract": "noesis-source-pack-runtime-coverage-v1",
            "domains": domains,
        }
