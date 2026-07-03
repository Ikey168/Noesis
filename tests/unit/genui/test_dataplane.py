"""Unit tests for the data-plane proxy support (src/genui/dataplane.py, R12).

Covers the data-mode allowlist discovery, the rate limiter, the size caps and
the invoke guardrails. All host access goes through a fake.
"""

import pytest

from src.genui import dataplane
from src.genui.dataplane import (
    DataPlaneError,
    RateLimiter,
    check_request_size,
    data_mode_tools,
    invoke_data_tool,
    is_allowed,
)


class FakeHost:
    def __init__(self, tools_by_server, results=None):
        self._tools = tools_by_server
        self._results = results or {}

    def tools(self, server=None):
        if server is not None:
            return {server: self._tools.get(server, [])}
        return dict(self._tools)

    def call_tool_cached(self, server, tool, arguments=None, **kw):
        return self._results[(server, tool)]


def _data_tool(name, panel="articles", has_schema=True):
    return {
        "name": name,
        "meta": {"data": {"panel": panel, "rest_route": "/api/v1/news/articles"}},
        "has_output_schema": has_schema,
    }


def _panel_tool(name):
    return {"name": name, "meta": {"panel": {"type": "articles"}}, "has_output_schema": True}


@pytest.fixture
def fake_host(monkeypatch):
    def _install(tools_by_server, results=None):
        host = FakeHost(tools_by_server, results)
        monkeypatch.setattr("src.mcp_host.get_host", lambda: host)
        return host

    return _install


@pytest.fixture
def no_host(monkeypatch):
    monkeypatch.setattr("src.mcp_host.get_host", lambda: None)


# --- allowlist discovery ----------------------------------------------------


def test_data_mode_tools_discovers_only_data_blocks(fake_host):
    fake_host({"srv": [_data_tool("articles_data"), _panel_tool("latest_articles")]})
    tools = data_mode_tools()
    assert list(tools) == [("srv", "articles_data")]
    assert tools[("srv", "articles_data")]["panel"] == "articles"


def test_data_mode_tool_requires_output_schema(fake_host):
    fake_host({"srv": [_data_tool("articles_data", has_schema=False)]})
    assert data_mode_tools() == {}


def test_no_host_means_empty_allowlist(no_host):
    assert data_mode_tools() == {}
    assert is_allowed("srv", "articles_data") is False


def test_is_allowed(fake_host):
    fake_host({"srv": [_data_tool("articles_data")]})
    assert is_allowed("srv", "articles_data") is True
    assert is_allowed("srv", "not_a_tool") is False


# --- rate limiter -----------------------------------------------------------


def test_rate_limiter_allows_burst_then_blocks():
    limiter = RateLimiter(rate_per_min=3)
    assert [limiter.allow("c", now=1000) for _ in range(3)] == [True, True, True]
    assert limiter.allow("c", now=1000) is False  # bucket empty


def test_rate_limiter_refills_over_time():
    limiter = RateLimiter(rate_per_min=60)  # 1 token/sec
    for _ in range(60):
        limiter.allow("c", now=1000)
    assert limiter.allow("c", now=1000) is False
    assert limiter.allow("c", now=1002) is True  # ~2 tokens refilled


def test_rate_limiter_is_per_client():
    limiter = RateLimiter(rate_per_min=1)
    assert limiter.allow("a", now=1000) is True
    assert limiter.allow("a", now=1000) is False
    assert limiter.allow("b", now=1000) is True  # separate bucket


# --- size caps --------------------------------------------------------------


def test_request_size_cap(monkeypatch):
    monkeypatch.setattr(dataplane, "MAX_REQUEST_BYTES", 50)
    with pytest.raises(DataPlaneError) as exc:
        check_request_size({"x": "y" * 100})
    assert exc.value.status == 413


def test_request_size_ok():
    check_request_size({"topic": "energy"})  # no raise


# --- invoke guardrails ------------------------------------------------------


def test_invoke_rejects_non_allowlisted(fake_host):
    fake_host({"srv": [_data_tool("articles_data")]})
    with pytest.raises(DataPlaneError) as exc:
        invoke_data_tool("srv", "evil_tool", {})
    assert exc.value.status == 403 and exc.value.code == "not_allowed"


def test_invoke_returns_payload(fake_host):
    fake_host(
        {"srv": [_data_tool("articles_data")]},
        results={("srv", "articles_data"): {"count": 2, "articles": [{"id": "a"}, {"id": "b"}]}},
    )
    out = invoke_data_tool("srv", "articles_data", {"limit": 2})
    assert out["count"] == 2


def test_invoke_maps_tool_error(fake_host):
    fake_host(
        {"srv": [_data_tool("articles_data")]},
        results={("srv", "articles_data"): {"error": "warehouse locked"}},
    )
    with pytest.raises(DataPlaneError) as exc:
        invoke_data_tool("srv", "articles_data", {})
    assert exc.value.status == 502 and exc.value.code == "tool_error"


def test_invoke_response_size_cap(fake_host, monkeypatch):
    monkeypatch.setattr(dataplane, "MAX_RESPONSE_BYTES", 20)
    fake_host(
        {"srv": [_data_tool("articles_data")]},
        results={("srv", "articles_data"): {"articles": [{"title": "x" * 100}]}},
    )
    with pytest.raises(DataPlaneError) as exc:
        invoke_data_tool("srv", "articles_data", {})
    assert exc.value.status == 413 and exc.value.code == "response_too_large"


def test_invoke_without_host(no_host):
    with pytest.raises(DataPlaneError) as exc:
        invoke_data_tool("srv", "articles_data", {})
    # not allowlisted (no host -> empty allowlist) is checked first.
    assert exc.value.status == 403


# --- pre-warm on generate (ADR-002 cold-path lever) -------------------------


class WarmHost(FakeHost):
    def __init__(self, tools_by_server):
        super().__init__(tools_by_server, results={})
        self.warmed = []

    def call_tool_cached(self, server, tool, arguments=None, **kw):
        self.warmed.append((server, tool, arguments))
        return {"count": 0, "articles": []}


def test_prewarm_warms_data_backed_panels(monkeypatch):
    monkeypatch.setenv("NOESIS_GENUI_DATA_PROXY", "on")
    host = WarmHost({"srv": [_data_tool("articles_data", panel="articles")]})
    monkeypatch.setattr("src.mcp_host.get_host", lambda: host)
    spec = {"panels": [{"type": "articles"}, {"type": "note"}, {"type": "claims"}]}
    n = dataplane.prewarm_from_spec(spec, background=False)
    assert n == 1  # only the articles panel has a data-mode tool
    assert host.warmed == [("srv", "articles_data", {})]  # empty args, matches the client


def test_prewarm_noop_when_flag_off(monkeypatch):
    monkeypatch.setenv("NOESIS_GENUI_DATA_PROXY", "off")
    host = WarmHost({"srv": [_data_tool("articles_data")]})
    monkeypatch.setattr("src.mcp_host.get_host", lambda: host)
    assert dataplane.prewarm_from_spec({"panels": [{"type": "articles"}]}, background=False) == 0
    assert host.warmed == []


def test_prewarm_dedupes_and_skips_unbacked(monkeypatch):
    monkeypatch.setenv("NOESIS_GENUI_DATA_PROXY", "on")
    host = WarmHost({"srv": [_data_tool("articles_data", panel="articles")]})
    monkeypatch.setattr("src.mcp_host.get_host", lambda: host)
    spec = {"panels": [{"type": "articles"}, {"type": "articles"}, {"type": "stance"}]}
    assert dataplane.prewarm_from_spec(spec, background=False) == 1
    assert len(host.warmed) == 1  # deduped
