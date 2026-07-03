"""M2.2: the data-plane warm-session pool. The proxy reuses R1's supervised
sessions; these cover the warmth signal (is_connected / warm_data_plane) and the
wait-for-warm gate that stops the generate pre-warm racing startup."""

import src.genui.dataplane as dp
from src.mcp_host.host import MCPHost


def test_host_is_connected_reflects_live_session():
    host = MCPHost(specs=[])
    assert host.is_connected("pipeline_mcp") is False
    host._sessions["pipeline_mcp"] = object()  # a live supervised session
    assert host.is_connected("pipeline_mcp") is True
    host._sessions.pop("pipeline_mcp")
    assert host.is_connected("pipeline_mcp") is False


class _StubHost:
    def __init__(self, connected):
        self._connected = set(connected)

    def is_connected(self, server):
        return server in self._connected


def _patch(monkeypatch, allowlist, connected):
    monkeypatch.setattr(dp, "data_mode_tools", lambda: allowlist)
    import src.mcp_host as mh

    monkeypatch.setattr(mh, "get_host", lambda: _StubHost(connected))


ALLOWLIST = {
    ("pipeline_mcp", "articles_data"): {"panel": "articles"},
    ("osint_mcp", "corroborate"): {"panel": "corroboration"},
}


def test_warm_data_plane_reports_per_server_warmth(monkeypatch):
    _patch(monkeypatch, ALLOWLIST, {"pipeline_mcp"})
    out = dp.warm_data_plane()
    assert out["count"] == 2
    assert out["servers"] == {"pipeline_mcp": True, "osint_mcp": False}
    assert out["warm_count"] == 1
    assert out["ready"] is False


def test_warm_data_plane_ready_when_all_warm(monkeypatch):
    _patch(monkeypatch, ALLOWLIST, {"pipeline_mcp", "osint_mcp"})
    out = dp.warm_data_plane()
    assert out["ready"] is True and out["warm_count"] == 2


def test_wait_warm_returns_immediately_when_ready(monkeypatch):
    _patch(monkeypatch, ALLOWLIST, {"pipeline_mcp", "osint_mcp"})
    out = dp.wait_warm(timeout=1.0)
    assert out["ready"] is True


def test_wait_warm_times_out_when_cold(monkeypatch):
    _patch(monkeypatch, ALLOWLIST, set())
    out = dp.wait_warm(timeout=0.1, interval=0.02)
    assert out["ready"] is False


def test_warm_data_plane_empty_when_no_data_tools(monkeypatch):
    _patch(monkeypatch, {}, set())
    out = dp.warm_data_plane()
    assert out["count"] == 0 and out["ready"] is False
