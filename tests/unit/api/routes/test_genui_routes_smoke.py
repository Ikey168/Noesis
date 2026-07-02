"""Smoke tests for src/api/routes/genui_routes.py.

Mounts ONLY the generative-UI router on a fresh FastAPI app and asserts
every endpoint answers. The route module is loaded BY PATH (never via
``src.api.routes``, whose __init__ eagerly imports heavy ML modules).
The warehouse probe and domain-pack registry are patched out on the loaded
module object, and the LLM planner is forced off via NOESIS_GENUI_LLM so no
network call can ever happen.
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

_spec = importlib.util.spec_from_file_location(
    "genui_routes_under_test", REPO / "src/api/routes/genui_routes.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


@pytest.fixture
def client(monkeypatch):
    # Hermetic adaptivity inputs: no DuckDB warehouse probe, no pack
    # registry, no live LLM planner, no MCP host (kill switch keeps the
    # mcp block deterministic regardless of the SDK being installed).
    monkeypatch.setenv("NOESIS_GENUI_LLM", "off")
    monkeypatch.setenv("NOESIS_MCP_HOST", "off")
    monkeypatch.setattr(mod, "resolve_availability", lambda: (None, "unknown"))
    monkeypatch.setattr(mod, "resolve_ui_flags", lambda: ({}, "packs"))
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /api/v1/ui/generate
# ---------------------------------------------------------------------------
def test_generate_returns_valid_spec(client):
    resp = client.post(
        "/api/v1/ui/generate", json={"intent": "overview of ai coverage"}
    )
    assert resp.status_code == 200
    body = resp.json()
    spec = body["spec"]
    assert spec["spec_version"] == "ui-spec-v1"
    assert isinstance(spec["panels"], list) and spec["panels"]
    meta = body["meta"]
    assert meta["generated_by"] == "heuristic"
    assert meta["availability_known"] is False
    assert meta["ui_flags"] == {}


def test_generate_empty_body_uses_defaults(client):
    resp = client.post("/api/v1/ui/generate", json={})
    assert resp.status_code == 200
    spec = resp.json()["spec"]
    assert spec["spec_version"] == "ui-spec-v1"
    assert spec["panels"]
    assert spec["intent"] == ""


def test_generate_accepts_valid_source_type(client):
    resp = client.post(
        "/api/v1/ui/generate",
        json={"intent": "latest research", "source_type": "news"},
    )
    assert resp.status_code == 200
    assert resp.json()["spec"]["source_type"] == "news"


def test_generate_rejects_invalid_source_type(client):
    resp = client.post(
        "/api/v1/ui/generate",
        json={"intent": "anything", "source_type": "carrier-pigeon"},
    )
    assert resp.status_code == 422


def test_generate_rejects_overlong_intent(client):
    resp = client.post("/api/v1/ui/generate", json={"intent": "x" * 501})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/ui/context
# ---------------------------------------------------------------------------
def test_context_returns_adaptive_inputs(client):
    resp = client.get("/api/v1/ui/context")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "ui_flags",
        "ui_flags_source",
        "availability",
        "availability_source",
        "availability_known",
        "llm",
        "mcp",
    }
    assert body["ui_flags"] == {}
    assert body["ui_flags_source"] == "packs"
    assert body["availability"] is None
    assert body["availability_source"] == "unknown"
    assert body["availability_known"] is False
    assert set(body["llm"]) == {"enabled", "provider"}
    assert body["llm"]["enabled"] is False
    assert body["llm"]["provider"] is None
    # R1: MCP host health block. Disabled here either by the repo-wide
    # TESTING short-circuit or by the fixture's kill switch.
    assert body["mcp"]["enabled"] is False
    assert body["mcp"]["reason"] in ("testing", "disabled by NOESIS_MCP_HOST")
    assert body["mcp"]["servers"] == {}


def test_context_reports_mcp_host_health(client, monkeypatch):
    """With a live host, the mcp block carries per-server status."""
    monkeypatch.setattr(
        mod,
        "host_status",
        lambda: {
            "enabled": True,
            "ttl_seconds": 60.0,
            "total": 2,
            "connected": 1,
            "servers": {
                "neuronews-kg": {
                    "state": "connected",
                    "tool_count": 9,
                    "last_seen": "2026-07-02T00:00:00+00:00",
                    "last_error": None,
                    "restarts": 0,
                    "cache_age_seconds": 1.2,
                },
                "neuronews-pipeline": {
                    "state": "down",
                    "tool_count": 0,
                    "last_seen": None,
                    "last_error": "ConnectionError: boom",
                    "restarts": 3,
                    "cache_age_seconds": None,
                },
            },
        },
    )
    body = client.get("/api/v1/ui/context").json()
    mcp = body["mcp"]
    assert mcp["enabled"] is True
    assert mcp["connected"] == 1 and mcp["total"] == 2
    assert mcp["servers"]["neuronews-kg"]["state"] == "connected"
    assert mcp["servers"]["neuronews-pipeline"]["state"] == "down"


# ---------------------------------------------------------------------------
# GET /api/v1/ui/panels
# ---------------------------------------------------------------------------
def test_panels_exposes_catalog(client):
    resp = client.get("/api/v1/ui/panels")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["panels"], list) and body["panels"]
    assert body["count"] == len(body["panels"])
    for panel in body["panels"]:
        assert "type" in panel
        assert "title" in panel
        assert "facets" in panel


# ---------------------------------------------------------------------------
# GET /api/v1/ui/telemetry (R3: pack-supplied ambient signal)
# ---------------------------------------------------------------------------
def test_telemetry_returns_pack_supplied_signal(client, monkeypatch):
    monkeypatch.setattr(
        mod,
        "pack_telemetry",
        lambda: {
            "signals": [{"label": "DOCS", "value": 12}],
            "movers": [{"label": "paper", "intent": "library documents about paper"}],
            "ticker": {"label": "NEW IN LIBRARY", "items": ["Doc title"]},
            "packs": ["library"],
        },
    )
    body = client.get("/api/v1/ui/telemetry").json()
    assert body["packs"] == ["library"]
    assert body["ticker"]["label"] == "NEW IN LIBRARY"


def test_telemetry_error_returns_500(client, monkeypatch):
    def boom():
        raise RuntimeError("telemetry broke")

    monkeypatch.setattr(mod, "pack_telemetry", boom)
    resp = client.get("/api/v1/ui/telemetry")
    assert resp.status_code == 500
    assert "UI telemetry failed" in resp.json()["detail"]


# R2 litmus (a): a new annotated server surfaces its panel type through
# /api/v1/ui/panels with zero genui code changes — only the host cache
# differs between this test and the static one below.
def test_panels_surfaces_discovered_type(client, monkeypatch):
    class FakeHost:
        _data = {
            "research-server": [
                {
                    "name": "citations",
                    "description": "Citation network for papers.",
                    "meta": {
                        "panel": {
                            "type": "citation_graph",
                            "title": "Citation graph",
                            "facets": ["entities", "library"],
                            "default_span": 6,
                        }
                    },
                    "has_output_schema": True,
                }
            ]
        }

        def tools(self, server=None):
            if server is not None:
                return {server: self._data.get(server, [])}
            return dict(self._data)

    monkeypatch.setattr("src.mcp_host.get_host", lambda: FakeHost())
    body = client.get("/api/v1/ui/panels").json()
    by_type = {p["type"]: p for p in body["panels"]}
    assert "citation_graph" in by_type
    assert by_type["citation_graph"]["source"] == "research-server"
    assert by_type["claims"]["source"] == "static"
    assert body["count"] == len(body["panels"])


# R2 litmus (b): with no discovery (host down/absent) the payload is
# byte-identical to the static catalog.
def test_panels_byte_identical_without_discovery(client, monkeypatch):
    monkeypatch.setattr("src.mcp_host.get_host", lambda: None)
    from src.genui.catalog import panel_catalog_dict

    body = client.get("/api/v1/ui/panels").json()
    static = panel_catalog_dict()
    assert body == {"panels": static, "count": len(static)}
