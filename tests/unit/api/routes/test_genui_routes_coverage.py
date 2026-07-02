"""Comprehensive coverage tests for src/api/routes/genui_routes.py.

Mounts the router on a fresh FastAPI app and exercises every branch:
usage-signal re-ranking, availability fallbacks (all-False / unknown),
ui_flag gating, the LLM planner path (spec returned / None fallback),
planner and validation failures (500), and the context/panels error paths.

The route module is loaded BY PATH (never via ``src.api.routes``, whose
__init__ eagerly imports heavy ML modules). ``data_availability``,
``merged_ui_flags``, ``plan``, ``plan_with_llm``, ``llm_config``,
``validate_spec`` and ``panel_catalog_dict`` are from-imported into the
route module's namespace, so every patch targets the loaded module object
(``mod``) via monkeypatch — state never leaks across tests or files.
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

from src.genui.catalog import PANEL_CATALOG  # noqa: E402  (stdlib-only module)
from src.genui.planner import plan as heuristic_plan  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "genui_routes_under_test", REPO / "src/api/routes/genui_routes.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

ALL_TABLES = sorted({t for p in PANEL_CATALOG for t in p.tables})


def _panel_types(body):
    return {p["type"] for p in body["spec"]["panels"]}


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Deterministic defaults; individual tests override per branch."""
    monkeypatch.setenv("NOESIS_GENUI_LLM", "off")
    monkeypatch.setattr(mod, "data_availability", lambda: None)
    monkeypatch.setattr(mod, "merged_ui_flags", lambda: {})


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /generate — usage signals
# ---------------------------------------------------------------------------
def test_generate_signals_dismissed_removed_pinned_added(client):
    resp = client.post(
        "/api/v1/ui/generate",
        json={
            "intent": "",
            "signals": {
                "pinned": ["claims"],
                "dismissed": ["trending"],
                "weights": {"kpi_row": 5},
            },
        },
    )
    assert resp.status_code == 200
    types = _panel_types(resp.json())
    assert "trending" not in types  # dismissed panels are removed
    assert "claims" in types  # pinned panels are appended when absent
    assert resp.json()["meta"]["generated_by"] == "heuristic"


# ---------------------------------------------------------------------------
# POST /generate — data availability
# ---------------------------------------------------------------------------
def test_generate_availability_all_false_keeps_only_tableless_panels(client, monkeypatch):
    monkeypatch.setattr(
        mod, "data_availability", lambda: {t: False for t in ALL_TABLES}
    )
    resp = client.post("/api/v1/ui/generate", json={"intent": "ai overview"})
    assert resp.status_code == 200
    body = resp.json()
    panels = body["spec"]["panels"]
    assert panels  # never an empty canvas
    assert body["meta"]["availability_known"] is True
    # Every warehouse-backed panel is dropped; survivors need no tables.
    tableless = {p.type for p in PANEL_CATALOG if not p.tables}
    assert all(p["type"] in tableless for p in panels)


def test_generate_availability_all_false_falls_back_for_warehouse_only_facet(client, monkeypatch):
    monkeypatch.setattr(
        mod, "data_availability", lambda: {t: False for t in ALL_TABLES}
    )
    # A stance/conflict intent selects only warehouse-backed panels, so the
    # fallback-overview path fires when every table is empty.
    resp = client.post("/api/v1/ui/generate", json={"intent": "stance conflicts"})
    assert resp.status_code == 200
    panels = resp.json()["spec"]["panels"]
    assert panels
    assert any(
        p["rationale"] == "fallback overview (no live data detected)" for p in panels
    )


def test_generate_availability_unknown(client, monkeypatch):
    monkeypatch.setattr(mod, "data_availability", lambda: None)
    resp = client.post("/api/v1/ui/generate", json={"intent": "ai overview"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["availability_known"] is False
    assert body["spec"]["panels"]


# ---------------------------------------------------------------------------
# POST /generate — ui_flags gating
# ---------------------------------------------------------------------------
def test_generate_ui_flag_hides_trending_panel(client, monkeypatch):
    monkeypatch.setattr(mod, "merged_ui_flags", lambda: {"trending": False})
    resp = client.post(
        "/api/v1/ui/generate", json={"intent": "trending topics this week"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["spec"]["panels"]
    assert "trending" not in _panel_types(body)
    assert body["meta"]["ui_flags"] == {"trending": False}


# ---------------------------------------------------------------------------
# POST /generate — LLM planner path
# ---------------------------------------------------------------------------
def test_generate_uses_llm_spec_when_planner_returns_one(client, monkeypatch):
    llm_spec = heuristic_plan("ai overview", availability=None, ui_flags={})
    llm_spec.generated_by = "llm"
    monkeypatch.setattr(mod, "plan_with_llm", lambda *a, **k: llm_spec)
    resp = client.post("/api/v1/ui/generate", json={"intent": "ai overview"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["generated_by"] == "llm"
    assert body["spec"]["generated_by"] == "llm"
    assert body["spec"]["panels"]


def test_generate_falls_back_to_heuristic_when_llm_returns_none(client, monkeypatch):
    monkeypatch.setattr(mod, "plan_with_llm", lambda *a, **k: None)
    resp = client.post("/api/v1/ui/generate", json={"intent": "ai overview"})
    assert resp.status_code == 200
    assert resp.json()["meta"]["generated_by"] == "heuristic"


# ---------------------------------------------------------------------------
# POST /generate — failure paths
# ---------------------------------------------------------------------------
def test_generate_planner_error_returns_500(client, monkeypatch):
    monkeypatch.setattr(mod, "plan_with_llm", lambda *a, **k: None)

    def boom(*args, **kwargs):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(mod, "plan", boom)
    resp = client.post("/api/v1/ui/generate", json={"intent": "ai overview"})
    assert resp.status_code == 500
    assert "UI generation failed" in resp.json()["detail"]


def test_generate_invalid_spec_returns_500(client, monkeypatch):
    monkeypatch.setattr(mod, "validate_spec", lambda spec_dict: ["boom"])
    resp = client.post("/api/v1/ui/generate", json={"intent": "ai overview"})
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "failed validation" in detail
    assert "boom" in detail


# ---------------------------------------------------------------------------
# GET /context
# ---------------------------------------------------------------------------
def test_context_llm_disabled(client):
    resp = client.get("/api/v1/ui/context")
    assert resp.status_code == 200
    body = resp.json()
    assert body["availability"] is None
    assert body["availability_known"] is False
    assert body["llm"] == {"enabled": False, "provider": None}


def test_context_llm_enabled(client, monkeypatch):
    monkeypatch.setattr(
        mod,
        "llm_config",
        lambda: {"provider": "anthropic", "model": "m", "api_key": "k"},
    )
    monkeypatch.setattr(mod, "data_availability", lambda: {"news_articles": True})
    resp = client.get("/api/v1/ui/context")
    assert resp.status_code == 200
    body = resp.json()
    assert body["llm"]["enabled"] is True
    assert body["llm"]["provider"] == "anthropic"
    assert "api_key" not in body["llm"]  # never leak credentials
    assert body["availability_known"] is True
    assert body["availability"] == {"news_articles": True}


def test_context_error_returns_500(client, monkeypatch):
    def boom():
        raise RuntimeError("warehouse down")

    monkeypatch.setattr(mod, "data_availability", boom)
    resp = client.get("/api/v1/ui/context")
    assert resp.status_code == 500
    assert "UI context failed" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /panels
# ---------------------------------------------------------------------------
def test_panels_error_returns_500(client, monkeypatch):
    def boom():
        raise RuntimeError("catalog broke")

    monkeypatch.setattr(mod, "panel_catalog_dict", boom)
    resp = client.get("/api/v1/ui/panels")
    assert resp.status_code == 500
    assert "UI panel catalog failed" in resp.json()["detail"]
