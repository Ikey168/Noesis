"""Unit tests for src/mcp_host/host.py.

Everything runs against scripted fake sessions: no MCP SDK usage, no
subprocesses. Timing-sensitive assertions poll with generous deadlines so
the suite stays stable under parallel CI load.
"""

import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

import src.mcp_host.host as host_mod
from src.mcp_host.config import ServerSpec
from src.mcp_host.host import (
    DOWN_AFTER_FAILURES,
    MCPHost,
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_DEGRADED,
    STATE_DOWN,
    backoff_delay,
    host_enabled,
    host_status,
    sdk_available,
    start_host,
    stop_host,
)

SPEC = ServerSpec(name="fake", command="python3", args=("tools/fake/server.py",))


def wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class ScriptedServer:
    """A controllable fake server: connect failures, kills, tool changes."""

    def __init__(self, tools=("alpha", "beta")):
        self.tools = list(tools)
        self.fail_connects = 0
        self.alive = True
        self.connects = 0
        self.tool_calls = 0
        self.call_result = SimpleNamespace(
            isError=False, structuredContent={"ok": True}
        )

    @contextlib.asynccontextmanager
    async def factory(self, spec):
        self.connects += 1
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise ConnectionError("connect refused")
        yield self

    async def list_tools(self):
        if not self.alive:
            raise ConnectionError("server killed")
        return SimpleNamespace(
            tools=[SimpleNamespace(name=n, description="d") for n in self.tools]
        )

    async def call_tool(self, name, arguments):
        if not self.alive:
            raise ConnectionError("server killed")
        self.tool_calls += 1
        return self.call_result


@pytest.fixture
def fast_backoff(monkeypatch):
    monkeypatch.setattr(host_mod, "BACKOFF_BASE", 0.02)
    monkeypatch.setattr(host_mod, "BACKOFF_CAP", 0.05)


@pytest.fixture
def make_host(fast_backoff):
    hosts = []

    def _make(server, ttl=0.05, specs=None):
        h = MCPHost(
            specs=specs if specs is not None else [SPEC],
            session_factory=server.factory,
            ttl_seconds=ttl,
        )
        hosts.append(h)
        return h

    yield _make
    for h in hosts:
        h.stop(timeout=3.0)


def state_of(h, name="fake"):
    return h.status()["servers"][name]["state"]


# ---------------------------------------------------------------------------
# #583: pool lifecycle
# ---------------------------------------------------------------------------


def test_connects_and_reports_connected(make_host):
    server = ScriptedServer()
    h = make_host(server)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    status = h.status()["servers"]["fake"]
    assert status["tool_count"] == 2
    assert status["last_seen"] is not None
    assert status["last_error"] is None
    assert status["restarts"] == 0
    assert h.tools("fake") == {
        "fake": [
            {
                "name": "alpha",
                "description": "d",
                "meta": {},
                "has_output_schema": False,
                "input_schema": {"type": "object"},
            },
            {
                "name": "beta",
                "description": "d",
                "meta": {},
                "has_output_schema": False,
                "input_schema": {"type": "object"},
            },
        ]
    }


def test_start_returns_immediately_even_when_connect_hangs(fast_backoff):
    @contextlib.asynccontextmanager
    async def hanging_factory(spec):
        await asyncio.Event().wait()
        yield None  # pragma: no cover - never reached

    h = MCPHost(specs=[SPEC], session_factory=hanging_factory, ttl_seconds=0.05)
    began = time.monotonic()
    h.start()
    assert time.monotonic() - began < 1.0
    # Status is a snapshot read: instant even while the connect hangs.
    began = time.monotonic()
    status = h.status()
    assert time.monotonic() - began < 0.5
    assert status["servers"]["fake"]["state"] == STATE_CONNECTING
    h.stop(timeout=3.0)
    assert not h._thread.is_alive()


def test_stop_joins_thread_cleanly(make_host):
    server = ScriptedServer()
    h = make_host(server)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    h.stop(timeout=3.0)
    assert not h._thread.is_alive()


def test_no_specs_never_spawns_a_thread():
    h = MCPHost(specs=[], session_factory=None, ttl_seconds=1)
    h.start()
    assert h._thread is None
    status = h.status()
    assert status["total"] == 0 and status["connected"] == 0
    h.stop()


def test_start_is_idempotent(make_host):
    server = ScriptedServer()
    h = make_host(server)
    h.start()
    thread = h._thread
    h.start()
    assert h._thread is thread


def test_backoff_delay_is_capped_exponential():
    assert backoff_delay(0) == 0.0
    assert backoff_delay(1) == 1.0
    assert backoff_delay(2) == 2.0
    assert backoff_delay(3) == 4.0
    assert backoff_delay(50) == 60.0


# ---------------------------------------------------------------------------
# #584: health transitions + discovery cache
# ---------------------------------------------------------------------------


def test_degraded_then_down_on_consecutive_failures(make_host):
    server = ScriptedServer()
    server.fail_connects = 10_000
    h = make_host(server)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_DEGRADED, timeout=3.0)
    assert wait_until(lambda: state_of(h) == STATE_DOWN)
    status = h.status()["servers"]["fake"]
    assert "ConnectionError" in status["last_error"]
    assert h.status()["connected"] == 0
    assert server.connects >= DOWN_AFTER_FAILURES


def test_kill_restart_cycle_reconnects_and_counts_restart(make_host):
    server = ScriptedServer()
    h = make_host(server)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)

    # Kill: the next health tick fails and the server leaves connected.
    server.alive = False
    assert wait_until(lambda: state_of(h) in (STATE_DEGRADED, STATE_DOWN))

    # While the server is down its tool set changes; the reconnect must
    # replace (invalidate) the cache, not serve the stale list.
    server.tools = ["gamma"]
    server.alive = True
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    assert wait_until(
        lambda: [t["name"] for t in h.tools("fake")["fake"]] == ["gamma"]
    )
    assert h.status()["servers"]["fake"]["restarts"] >= 1


def test_ttl_refresh_updates_cache_within_one_ttl(make_host):
    server = ScriptedServer(tools=("one",))
    h = make_host(server, ttl=0.05)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    server.tools = ["one", "two"]
    assert wait_until(lambda: h.status()["servers"]["fake"]["tool_count"] == 2)
    age = h.status()["servers"]["fake"]["cache_age_seconds"]
    assert age is not None and age < 5


def test_kill_reflected_within_one_ttl(make_host):
    ttl = 0.05
    server = ScriptedServer()
    h = make_host(server, ttl=ttl)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    server.alive = False
    began = time.monotonic()
    assert wait_until(lambda: state_of(h) != STATE_CONNECTED, timeout=3.0)
    # Detection happens on the next TTL tick (generous bound for CI load).
    assert time.monotonic() - began < 2.0


def test_tools_accessor_shapes(make_host):
    server = ScriptedServer(tools=("a",))
    h = make_host(server)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    assert set(h.tools()) == {"fake"}
    assert h.tools("missing") == {}


def test_legacy_alias_reuses_canonical_session_and_cache(make_host):
    server = ScriptedServer(tools=("corroborate",))
    spec = ServerSpec(
        name="noesis-osint",
        command="python3",
        args=("tools/osint_mcp/server.py",),
        aliases=("neuronews-osint",),
    )
    h = make_host(server, specs=[spec])
    h.start()
    assert wait_until(lambda: state_of(h, "noesis-osint") == STATE_CONNECTED)

    with pytest.warns(DeprecationWarning, match="noesis-osint"):
        first = h.call_tool_cached("neuronews-osint", "corroborate")
    second = h.call_tool_cached("noesis-osint", "corroborate")
    assert first == second == {"ok": True}
    assert server.tool_calls == 1

    with pytest.warns(DeprecationWarning, match="noesis-osint"):
        assert h.tools("neuronews-osint") == h.tools("noesis-osint")
    assert h.status()["servers"]["noesis-osint"]["aliases"] == [
        "neuronews-osint"
    ]


def test_ttl_env_override(monkeypatch):
    monkeypatch.setenv("NOESIS_MCP_TTL", "7")
    assert MCPHost(specs=[]).ttl_seconds == 7.0
    monkeypatch.setenv("NOESIS_MCP_TTL", "junk")
    assert MCPHost(specs=[]).ttl_seconds == host_mod.DEFAULT_TTL_SECONDS


# ---------------------------------------------------------------------------
# call_tool (R3: stats tools feed adaptivity)
# ---------------------------------------------------------------------------


def test_call_tool_returns_structured_content(make_host):
    server = ScriptedServer()
    h = make_host(server)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    assert h.call_tool("fake", "anything", {"x": 1}) == {"ok": True}


def test_call_tool_requires_running_loop():
    h = MCPHost(specs=[SPEC], session_factory=None, ttl_seconds=1)
    with pytest.raises(RuntimeError, match="not running"):
        h.call_tool("fake", "anything")


def test_call_tool_requires_live_session(make_host):
    server = ScriptedServer()
    h = make_host(server)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    with pytest.raises(RuntimeError, match="no live session"):
        h.call_tool("other-server", "anything")


def test_call_tool_raises_on_tool_error(make_host):
    server = ScriptedServer()
    server.call_result = SimpleNamespace(
        isError=True, structuredContent=None, content="boom"
    )
    h = make_host(server)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    with pytest.raises(RuntimeError, match="returned an error"):
        h.call_tool("fake", "anything")


def test_call_tool_non_dict_content_becomes_empty(make_host):
    server = ScriptedServer()
    server.call_result = SimpleNamespace(isError=False, structuredContent=[1, 2])
    h = make_host(server)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    assert h.call_tool("fake", "anything") == {}


# ---------------------------------------------------------------------------
# call_tool_cached (R4 #593: shared stats cache)
# ---------------------------------------------------------------------------


def test_call_tool_cached_caches_within_ttl(make_host):
    server = ScriptedServer()
    server.call_result = SimpleNamespace(isError=False, structuredContent={"v": 1})
    h = make_host(server)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)

    assert h.call_tool_cached("fake", "t", {"a": 1}) == {"v": 1}
    n = server.tool_calls
    # Same (server, tool, args): served from cache, no new round-trip.
    assert h.call_tool_cached("fake", "t", {"a": 1}) == {"v": 1}
    assert server.tool_calls == n
    # Different args are a different cache key.
    h.call_tool_cached("fake", "t", {"a": 2})
    assert server.tool_calls == n + 1


def test_invalidate_cached_calls(make_host):
    server = ScriptedServer()
    server.call_result = SimpleNamespace(isError=False, structuredContent={"v": 1})
    h = make_host(server)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    h.call_tool_cached("fake", "t")
    n = server.tool_calls
    h.invalidate_cached_calls("fake")
    h.call_tool_cached("fake", "t")
    assert server.tool_calls == n + 1
    # Invalidate-all also clears.
    h.invalidate_cached_calls()
    h.call_tool_cached("fake", "t")
    assert server.tool_calls == n + 2


def test_reconnect_invalidates_call_cache(make_host):
    server = ScriptedServer()
    server.call_result = SimpleNamespace(isError=False, structuredContent={"v": 1})
    h = make_host(server, ttl=0.05)
    h.start()
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    assert h.call_tool_cached("fake", "t") == {"v": 1}

    # Kill; on reconnect the server reports a new value, and the stale cache
    # entry must be dropped so the fresh value is served.
    server.alive = False
    assert wait_until(lambda: state_of(h) != STATE_CONNECTED)
    server.call_result = SimpleNamespace(isError=False, structuredContent={"v": 2})
    server.alive = True
    assert wait_until(lambda: h.status()["servers"]["fake"]["restarts"] >= 1)
    assert wait_until(lambda: state_of(h) == STATE_CONNECTED)
    assert h.call_tool_cached("fake", "t") == {"v": 2}


# ---------------------------------------------------------------------------
# Module-level singleton + short circuits
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("NOESIS_MCP_HOST", raising=False)
    yield monkeypatch
    stop_host()


def test_testing_env_short_circuits(clean_env):
    clean_env.setenv("TESTING", "true")
    assert host_enabled() == "testing"
    assert start_host() is None
    status = host_status()
    assert status["enabled"] is False and status["reason"] == "testing"
    assert status["servers"] == {}


def test_kill_switch_short_circuits(clean_env):
    clean_env.setenv("NOESIS_MCP_HOST", "off")
    assert host_enabled() == "disabled by NOESIS_MCP_HOST"
    assert start_host() is None
    assert host_status()["enabled"] is False


def test_missing_sdk_short_circuits(clean_env):
    clean_env.setattr(host_mod, "sdk_available", lambda: False)
    assert host_enabled() == "mcp client SDK not installed"
    assert host_status()["reason"] == "mcp client SDK not installed"


def test_singleton_start_status_stop(clean_env):
    class StubHost:
        started = 0
        stopped = 0

        def start(self):
            StubHost.started += 1

        def stop(self, timeout=5.0):
            StubHost.stopped += 1

        def status(self):
            return {"enabled": True, "servers": {}, "total": 0, "connected": 0}

    clean_env.setattr(host_mod, "sdk_available", lambda: True)
    clean_env.setattr(host_mod, "MCPHost", StubHost)
    assert host_status()["reason"] == "not started"

    first = start_host()
    second = start_host()
    assert first is second and StubHost.started == 1
    assert host_status()["enabled"] is True

    stop_host()
    assert StubHost.stopped == 1
    assert host_status()["reason"] == "not started"
    stop_host()  # idempotent
    assert StubHost.stopped == 1


def test_sdk_available_returns_bool():
    assert isinstance(sdk_available(), bool)
