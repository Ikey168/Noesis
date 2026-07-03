"""
Data-plane proxy support (MCP rearchitecture plan, R12 / Stage 3 gate).

Stage 1 turned MCP tools into panels; Stage 3 asks whether panel *data* should
flow through MCP too. This module is the backend for a prototype answer: a
narrow, guardrailed way for the browser to have the API invoke a **data-mode**
MCP tool on its behalf (the browser never speaks MCP).

* :func:`data_mode_tools` discovers the data-mode allowlist from the R1 tool
  cache: a tool is data-mode when its ``meta`` carries a ``data`` block (format
  mirrors the ADR-001 ``panel`` block). Only allowlisted tools are callable.
* :class:`RateLimiter` is a per-client token bucket.
* :func:`invoke_data_tool` enforces the allowlist and the response-size cap and
  calls the tool through the host's shared cache.

Everything is behind the ``NOESIS_GENUI_DATA_PROXY`` feature flag; the whole
prototype is off by default. Import-safe (stdlib + src.mcp_host only).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, Optional, Tuple

DATA_ANNOTATION_KEY = "data"

# Caps (bytes). Requests/responses beyond these are rejected, not truncated.
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Per-client rate limit defaults (token bucket).
DEFAULT_RATE_PER_MIN = 120


class DataPlaneError(Exception):
    """A data-plane request was refused. ``status`` is the HTTP code to map to."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def data_proxy_enabled() -> bool:
    """True when the data-plane proxy prototype is switched on."""
    return os.getenv("NOESIS_GENUI_DATA_PROXY", "off").lower() in ("on", "1", "true")


def data_mode_tools() -> Dict[Tuple[str, str], Dict[str, Any]]:
    """The data-mode allowlist: ``{(server, tool): {panel, rest_route}}`` from
    the host's tool cache. Empty when the host is down or nothing is annotated,
    so an empty allowlist safely rejects everything."""
    try:
        from src.mcp_host import get_host

        host = get_host()
    except Exception:  # pragma: no cover - defensive import guard
        return {}
    if host is None:
        return {}

    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for server in sorted(host.tools()):
        for tool in host.tools(server).get(server, []):
            meta = tool.get("meta")
            block = meta.get(DATA_ANNOTATION_KEY) if isinstance(meta, dict) else None
            if not isinstance(block, dict):
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            if not tool.get("has_output_schema", False):
                # Data-mode tools must declare a schema, same discipline as panels.
                continue
            out[(server, name)] = {
                "panel": block.get("panel"),
                "rest_route": block.get("rest_route"),
            }
    return out


def is_allowed(server: str, tool: str) -> bool:
    """Whether ``(server, tool)`` is an allowlisted data-mode tool."""
    return (server, tool) in data_mode_tools()


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """A per-client token bucket: ``rate`` requests per minute, burst = rate."""

    def __init__(self, rate_per_min: int = DEFAULT_RATE_PER_MIN):
        self.rate = max(1, int(rate_per_min))
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = Lock()

    def allow(self, client: str, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        refill_per_sec = self.rate / 60.0
        with self._lock:
            bucket = self._buckets.get(client)
            if bucket is None:
                self._buckets[client] = _Bucket(tokens=self.rate - 1, updated=now)
                return True
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(self.rate, bucket.tokens + elapsed * refill_per_sec)
            bucket.updated = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False


# One shared limiter for the process; the rate is read once at import.
_LIMITER = RateLimiter(
    int(os.getenv("NOESIS_GENUI_DATA_RATE_PER_MIN", str(DEFAULT_RATE_PER_MIN)))
)


def check_rate(client: str) -> None:
    """Raise :class:`DataPlaneError` (429) when ``client`` is over its limit."""
    if not _LIMITER.allow(client):
        raise DataPlaneError(429, "rate_limited", "data-plane rate limit exceeded")


def check_request_size(arguments: Any) -> None:
    """Raise (413) when the argument payload exceeds the request cap."""
    import json

    try:
        size = len(json.dumps(arguments or {}, default=str).encode("utf-8"))
    except Exception:
        raise DataPlaneError(400, "bad_request", "arguments are not JSON-serializable")
    if size > MAX_REQUEST_BYTES:
        raise DataPlaneError(413, "request_too_large",
                             f"request {size} bytes over {MAX_REQUEST_BYTES} cap")


def _data_tool_for_panel(panel_type: str) -> Optional[Tuple[str, str]]:
    """The ``(server, tool)`` data-mode tool that serves a panel type, or None."""
    for (server, tool), meta in data_mode_tools().items():
        if meta.get("panel") == panel_type:
            return (server, tool)
    return None


def warm_data_plane() -> Dict[str, Any]:
    """Warmth summary for the data-plane servers (M2.2): which MCP servers that
    back a data-mode panel currently hold a live (warm) session the proxy can
    reuse. A ``/ui/data`` request against a warm server skips the connect cost;
    a request against a cold server pays it (or falls back). ``ready`` is True
    only when every data-plane server is warm."""
    servers = sorted({server for (server, _tool) in data_mode_tools()})
    try:
        from src.mcp_host import get_host

        host = get_host()
    except Exception:  # pragma: no cover - defensive import guard
        host = None
    warm: Dict[str, bool] = {}
    for server in servers:
        warm[server] = bool(
            host is not None
            and hasattr(host, "is_connected")
            and host.is_connected(server)
        )
    return {
        "ready": bool(servers) and all(warm.values()),
        "servers": warm,
        "count": len(servers),
        "warm_count": sum(1 for v in warm.values() if v),
    }


def wait_warm(timeout: float = 2.0, interval: float = 0.05) -> Dict[str, Any]:
    """Poll :func:`warm_data_plane` until every data-plane server is warm or the
    timeout elapses. Returns the final warmth summary. Used to gate the cold
    path: the first fetch (or the generate pre-warm) can wait briefly for the
    supervised sessions to come up instead of racing startup and missing."""
    deadline = time.time() + max(0.0, timeout)
    summary = warm_data_plane()
    while not summary["ready"] and time.time() < deadline:
        time.sleep(max(0.001, interval))
        summary = warm_data_plane()
    return summary


def prewarm_from_spec(spec: Dict[str, Any], background: bool = True) -> int:
    """Warm the shared tool cache for every panel in a spec that a data-mode
    tool can serve, so the browser's first ``/ui/data`` fetch is a cache hit
    (the ADR-002 cold-path lever: pre-warm on generate).

    Returns the number of distinct data-mode tools scheduled. Best-effort:
    warming errors are swallowed, and with ``background`` (the default) the
    warming runs on a daemon thread so it never adds latency to the generate
    response. Tests pass ``background=False`` to warm synchronously.
    """
    if not data_proxy_enabled():
        return 0
    panels = spec.get("panels") if isinstance(spec, dict) else None
    if not isinstance(panels, list):
        return 0

    targets: list[Tuple[str, str]] = []
    seen = set()
    for panel in panels:
        ptype = panel.get("type") if isinstance(panel, dict) else None
        if not ptype:
            continue
        target = _data_tool_for_panel(ptype)
        if target and target not in seen:
            seen.add(target)
            targets.append(target)
    if not targets:
        return 0

    def _warm() -> None:
        try:
            from src.mcp_host import get_host

            host = get_host()
        except Exception:
            host = None
        if host is None:
            return
        # M2.2: wait briefly for the supervised sessions to be warm before
        # warming the cache, so a startup race does not make the pre-warm miss
        # (a cold session would drop the warming call and the browser's first
        # fetch would still pay the connect cost).
        wait_warm(timeout=2.0)
        for server, tool in targets:
            try:
                # Same empty-args call the browser makes, so the cache key matches.
                host.call_tool_cached(server, tool, {})
            except Exception:
                continue  # best-effort; a cold miss just falls back to the live path

    if background:
        import threading

        threading.Thread(target=_warm, name="dataplane-prewarm", daemon=True).start()
    else:
        _warm()
    return len(targets)


# Lighter-encoding threshold (M2.1): payloads at or above this size are worth
# gzip-compressing; smaller ones ship raw so tiny responses pay no CPU cost.
COMPRESS_MIN_BYTES = 2 * 1024


def encode_payload(body: Any, accept_encoding: str = "") -> Tuple[bytes, Optional[str]]:
    """Serialize a data-plane response body to compact JSON bytes, gzip-compressing
    it when the client accepts gzip and the payload is large enough to benefit.

    Returns ``(data_bytes, content_encoding)`` where ``content_encoding`` is
    ``"gzip"`` or ``None``. Pure (stdlib only) so the route stays thin and this
    is unit-testable without FastAPI. The compact separators drop inter-token
    whitespace even on the uncompressed path.
    """
    import gzip
    import json

    raw = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
    if len(raw) >= COMPRESS_MIN_BYTES and "gzip" in (accept_encoding or "").lower():
        return gzip.compress(raw, compresslevel=6), "gzip"
    return raw, None


def invoke_data_tool(
    server: str, tool: str, arguments: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Invoke an allowlisted data-mode tool through the host cache, enforcing
    the allowlist and the response-size cap. Raises :class:`DataPlaneError`."""
    import json

    if not is_allowed(server, tool):
        raise DataPlaneError(403, "not_allowed",
                             f"tool {server}:{tool} is not an allowlisted data-mode tool")
    try:
        from src.mcp_host import get_host

        host = get_host()
    except Exception:
        host = None
    if host is None:
        raise DataPlaneError(503, "host_unavailable", "MCP host is not available")

    try:
        result = host.call_tool_cached(server, tool, arguments or {})
    except Exception as exc:
        raise DataPlaneError(502, "tool_error", f"tool call failed: {exc}")

    if not isinstance(result, dict):
        raise DataPlaneError(502, "bad_result", "tool returned a non-object result")
    if "error" in result:
        raise DataPlaneError(502, "tool_error", str(result["error"]))

    size = len(json.dumps(result, default=str).encode("utf-8"))
    if size > MAX_RESPONSE_BYTES:
        raise DataPlaneError(413, "response_too_large",
                             f"response {size} bytes over {MAX_RESPONSE_BYTES} cap")
    return result
