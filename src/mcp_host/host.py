"""
The MCP host runtime (MCP rearchitecture plan, R1).

Pooled, supervised FastMCP stdio sessions inside the API process:

* **Lazy connect** — ``MCPHost.start()`` only spawns a daemon thread with a
  private asyncio loop; the actual connects happen in background tasks, so
  API boot time is unaffected.
* **Supervision** — one task per server keeps a session alive, reconnecting
  with capped exponential backoff. State transitions: connecting, then
  connected, degraded on the first failure, down after
  ``DOWN_AFTER_FAILURES`` consecutive failures.
* **Discovery cache** — ``tools/list`` results are cached per server and
  refreshed on a TTL; the cache is replaced (invalidated) on every
  reconnect. Readers never trigger a live round-trip.
* **Non-blocking status** — ``status()`` and ``tools()`` read a snapshot
  guarded by a plain lock; they never await anything, so a hung server can
  never stall a request (``GET /api/v1/ui/context`` reads this).

No planning-behavior changes live here: this is the risky infrastructure,
isolated, per the plan.

The default session factory uses the ``mcp`` client SDK, imported lazily;
when the SDK is missing the host reports itself unavailable instead of
breaking imports. Unit tests inject fake session factories and never spawn
processes.

Environment:

* ``NOESIS_MCP_HOST``  — ``auto`` (default) or ``off``/``0``/``false``.
* ``NOESIS_MCP_TTL``   — discovery-cache TTL seconds (default 60).
* ``TESTING``          — truthy short-circuits ``start_host()`` entirely.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from src.mcp_host.config import ServerSpec, load_server_specs

logger = logging.getLogger(__name__)

# States a supervised server moves through.
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_DEGRADED = "degraded"
STATE_DOWN = "down"

DEFAULT_TTL_SECONDS = 60.0
CONNECT_TIMEOUT = 15.0
CALL_TIMEOUT = 10.0
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
DOWN_AFTER_FAILURES = 3
# Stagger initial connects so 12 subprocesses don't spawn in one burst.
CONNECT_STAGGER = 0.1


def backoff_delay(consecutive_failures: int) -> float:
    """Capped exponential backoff for reconnect attempts."""
    if consecutive_failures <= 0:
        return 0.0
    return min(BACKOFF_CAP, BACKOFF_BASE * (2 ** (consecutive_failures - 1)))


def _utc_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class _ServerStatus:
    """Mutable per-server snapshot (guarded by MCPHost._lock)."""

    state: str = STATE_CONNECTING
    last_seen: Optional[float] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    restarts: int = 0
    tool_count: int = 0
    tools: List[Dict[str, str]] = field(default_factory=list)
    tools_refreshed: Optional[float] = None


@contextlib.asynccontextmanager
async def _default_session(spec: ServerSpec):
    """Connect a real stdio session via the ``mcp`` client SDK.

    Imported lazily so the host degrades gracefully (instead of breaking
    API imports) when the SDK is not installed.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=spec.command,
        args=list(spec.args),
        env={**os.environ, **spec.env},
        cwd=spec.cwd,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), CONNECT_TIMEOUT)
            yield session


async def _list_tools(session: Any) -> List[Dict[str, Any]]:
    """One health-check round-trip; doubles as the discovery refresh.

    Each cached tool carries its ``_meta`` block and whether it declares an
    ``outputSchema`` — the two things the R2 discovery-derived catalog
    (src/genui/discovery.py) needs to map annotated tools into PanelDefs.
    """
    result = await asyncio.wait_for(session.list_tools(), CALL_TIMEOUT)
    tools: List[Dict[str, Any]] = []
    for tool in result.tools:
        meta = getattr(tool, "meta", None)
        tools.append(
            {
                "name": tool.name,
                "description": (tool.description or "").strip(),
                "meta": meta if isinstance(meta, dict) else {},
                "has_output_schema": getattr(tool, "outputSchema", None) is not None,
            }
        )
    return tools


def sdk_available() -> bool:
    """True when the ``mcp`` client SDK is importable."""
    try:
        import mcp  # noqa: F401

        return True
    except Exception:
        return False


class MCPHost:
    """Supervises the project's MCP servers from a background thread."""

    def __init__(
        self,
        specs: Optional[List[ServerSpec]] = None,
        session_factory: Optional[Callable[[ServerSpec], Any]] = None,
        ttl_seconds: Optional[float] = None,
    ):
        self.specs = list(specs) if specs is not None else load_server_specs()
        self._session_factory = session_factory or _default_session
        env_ttl = os.getenv("NOESIS_MCP_TTL", "").strip()
        if ttl_seconds is not None:
            self.ttl_seconds = float(ttl_seconds)
        else:
            try:
                self.ttl_seconds = float(env_ttl) if env_ttl else DEFAULT_TTL_SECONDS
            except ValueError:
                self.ttl_seconds = DEFAULT_TTL_SECONDS

        self._lock = threading.Lock()
        self._statuses: Dict[str, _ServerStatus] = {
            spec.name: _ServerStatus() for spec in self.specs
        }
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Created loop-less here (safe since 3.10) so stop() always has an
        # object to signal, even mid-startup.
        self._stop_event = asyncio.Event()
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the supervisor thread; returns immediately (lazy connect)."""
        if self._started or not self.specs:
            self._started = True
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._run_loop, name="mcp-host", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the supervisor loop to exit and join the thread.

        Tolerates being called while the loop thread is still starting up:
        it polls until the loop is running before signalling.
        """
        deadline = time.monotonic() + timeout
        while self._thread is not None and self._thread.is_alive():
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._stop_event.set)
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._started = False

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._supervise_all())
        except Exception:  # pragma: no cover - defensive; must never kill the API
            logger.exception("MCP host supervisor crashed")
        finally:
            with contextlib.suppress(Exception):
                loop.close()

    async def _supervise_all(self) -> None:
        tasks = [
            asyncio.create_task(self._supervise(spec, index))
            for index, spec in enumerate(self.specs)
        ]
        await self._stop_event.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- per-server supervision --------------------------------------------

    async def _supervise(self, spec: ServerSpec, index: int) -> None:
        """Keep one server connected; reconnect with capped backoff."""
        await asyncio.sleep(index * CONNECT_STAGGER)
        first_connect = True
        while not self._stop_event.is_set():
            try:
                async with self._session_factory(spec) as session:
                    # Reconnect invalidates the discovery cache: the fresh
                    # tools/list result replaces whatever was cached.
                    tools = await _list_tools(session)
                    self._mark_connected(spec.name, tools, reconnected=not first_connect)
                    first_connect = False
                    while not self._stop_event.is_set():
                        await self._sleep_or_stop(self.ttl_seconds)
                        if self._stop_event.is_set():
                            break
                        tools = await _list_tools(session)
                        self._mark_connected(spec.name, tools)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                failures = self._mark_failed(spec.name, err)
                await self._sleep_or_stop(backoff_delay(failures))
        return None

    async def _sleep_or_stop(self, delay: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(delay, 0.001))

    # -- status bookkeeping --------------------------------------------------

    def _mark_connected(
        self, name: str, tools: List[Dict[str, str]], reconnected: bool = False
    ) -> None:
        now = time.time()
        with self._lock:
            status = self._statuses[name]
            status.state = STATE_CONNECTED
            status.last_seen = now
            status.last_error = None
            status.consecutive_failures = 0
            status.tools = tools
            status.tool_count = len(tools)
            status.tools_refreshed = now
            if reconnected:
                status.restarts += 1

    def _mark_failed(self, name: str, err: Exception) -> int:
        with self._lock:
            status = self._statuses[name]
            status.consecutive_failures += 1
            status.last_error = f"{type(err).__name__}: {err}"[:300]
            status.state = (
                STATE_DOWN
                if status.consecutive_failures >= DOWN_AFTER_FAILURES
                else STATE_DEGRADED
            )
            return status.consecutive_failures

    # -- non-blocking readers ------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Snapshot for /api/v1/ui/context; never awaits or blocks on IO."""
        now = time.time()
        with self._lock:
            servers = {
                name: {
                    "state": s.state,
                    "tool_count": s.tool_count,
                    "last_seen": _utc_iso(s.last_seen),
                    "last_error": s.last_error,
                    "restarts": s.restarts,
                    "cache_age_seconds": (
                        round(now - s.tools_refreshed, 1)
                        if s.tools_refreshed is not None
                        else None
                    ),
                }
                for name, s in self._statuses.items()
            }
        connected = sum(1 for s in servers.values() if s["state"] == STATE_CONNECTED)
        return {
            "enabled": True,
            "ttl_seconds": self.ttl_seconds,
            "total": len(servers),
            "connected": connected,
            "servers": servers,
        }

    def tools(self, server: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """Cached discovery results per server (name, description, meta,
        has_output_schema). Snapshot read; never triggers a round-trip."""
        with self._lock:
            if server is not None:
                status = self._statuses.get(server)
                return {server: list(status.tools)} if status else {}
            return {name: list(s.tools) for name, s in self._statuses.items()}


# -- module-level singleton ---------------------------------------------------

_host: Optional[MCPHost] = None
_host_lock = threading.Lock()


def _disabled_status(reason: str) -> Dict[str, Any]:
    return {"enabled": False, "reason": reason, "servers": {}, "total": 0, "connected": 0}


def host_enabled() -> Optional[str]:
    """None when the host may run; otherwise the reason it must not."""
    if os.environ.get("TESTING", "").strip().lower() in ("1", "true", "yes"):
        return "testing"
    if os.getenv("NOESIS_MCP_HOST", "auto").strip().lower() in ("off", "0", "false"):
        return "disabled by NOESIS_MCP_HOST"
    if not sdk_available():
        return "mcp client SDK not installed"
    return None


def start_host() -> Optional[MCPHost]:
    """Create and start the singleton host (no-op under TESTING / kill switch)."""
    global _host
    if host_enabled() is not None:
        return None
    with _host_lock:
        if _host is None:
            _host = MCPHost()
            _host.start()
        return _host


def stop_host() -> None:
    """Stop and drop the singleton host (used by app shutdown and tests)."""
    global _host
    with _host_lock:
        host, _host = _host, None
    if host is not None:
        host.stop()


def get_host() -> Optional[MCPHost]:
    return _host


def host_status() -> Dict[str, Any]:
    """The ``mcp`` block for /api/v1/ui/context; always safe to call."""
    reason = host_enabled()
    if reason is not None:
        return _disabled_status(reason)
    host = _host
    if host is None:
        return _disabled_status("not started")
    return host.status()
