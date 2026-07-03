"""Route tests for src/api/routes/genui_data_routes.py (R12 data-plane proxy).

Loads the route module BY PATH (never via src.api.routes, whose __init__
eagerly imports heavy ML modules), mounts only that router, and drives it with
a fake host so no MCP session is needed. Covers the flag gate, the allowlist,
and the rate-limit / size-cap rejections.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.genui import dataplane  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "genui_data_routes_under_test", REPO / "src/api/routes/genui_data_routes.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class FakeHost:
    def __init__(self, tools, results):
        self._tools = tools
        self._results = results

    def tools(self, server=None):
        if server is not None:
            return {server: self._tools.get(server, [])}
        return dict(self._tools)

    def call_tool_cached(self, server, tool, arguments=None, **kw):
        return self._results[(server, tool)]


def _data_tool(name):
    return {
        "name": name,
        "meta": {"data": {"panel": "articles", "rest_route": "/api/v1/news/articles"}},
        "has_output_schema": True,
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("NOESIS_GENUI_DATA_PROXY", "on")
    # Fresh limiter each test so bursts do not leak across tests.
    monkeypatch.setattr(dataplane, "_LIMITER", dataplane.RateLimiter(1000))
    host = FakeHost(
        {"neuronews-pipeline": [_data_tool("articles_data")]},
        {("neuronews-pipeline", "articles_data"): {"count": 1, "articles": [{"id": "a1"}]}},
    )
    monkeypatch.setattr("src.mcp_host.get_host", lambda: host)
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def test_disabled_returns_404(monkeypatch):
    monkeypatch.setenv("NOESIS_GENUI_DATA_PROXY", "off")
    app = FastAPI()
    app.include_router(mod.router)
    c = TestClient(app)
    assert c.post("/api/v1/ui/data", json={"server": "x", "tool": "y"}).status_code == 404
    tools = c.get("/api/v1/ui/data/tools").json()
    assert tools["enabled"] is False and tools["tools"] == []


def test_allowlist_is_exposed(client):
    body = client.get("/api/v1/ui/data/tools").json()
    assert body["enabled"] is True
    assert body["count"] == 1
    assert body["tools"][0]["tool"] == "articles_data"
    assert body["tools"][0]["panel"] == "articles"


def test_allowed_tool_returns_payload(client):
    resp = client.post(
        "/api/v1/ui/data",
        json={"server": "neuronews-pipeline", "tool": "articles_data", "arguments": {"limit": 1}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool"] == "articles_data"
    assert body["data"]["count"] == 1


def test_non_allowlisted_tool_is_forbidden(client):
    resp = client.post(
        "/api/v1/ui/data",
        json={"server": "neuronews-pipeline", "tool": "trigger_delete_everything"},
    )
    assert resp.status_code == 403


def test_rate_limit_returns_429(client, monkeypatch):
    monkeypatch.setattr(dataplane, "_LIMITER", dataplane.RateLimiter(2))
    ok1 = client.post("/api/v1/ui/data", json={"server": "neuronews-pipeline", "tool": "articles_data"})
    ok2 = client.post("/api/v1/ui/data", json={"server": "neuronews-pipeline", "tool": "articles_data"})
    blocked = client.post("/api/v1/ui/data", json={"server": "neuronews-pipeline", "tool": "articles_data"})
    assert ok1.status_code == 200 and ok2.status_code == 200
    assert blocked.status_code == 429


def test_oversized_request_returns_413(client, monkeypatch):
    monkeypatch.setattr(dataplane, "MAX_REQUEST_BYTES", 40)
    resp = client.post(
        "/api/v1/ui/data",
        json={"server": "neuronews-pipeline", "tool": "articles_data", "arguments": {"blob": "x" * 200}},
    )
    assert resp.status_code == 413
