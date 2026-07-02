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
    # registry, no live LLM planner.
    monkeypatch.setenv("NOESIS_GENUI_LLM", "off")
    monkeypatch.setattr(mod, "data_availability", lambda: None)
    monkeypatch.setattr(mod, "merged_ui_flags", lambda: {})
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
    assert set(body) == {"ui_flags", "availability", "availability_known", "llm"}
    assert body["ui_flags"] == {}
    assert body["availability"] is None
    assert body["availability_known"] is False
    assert set(body["llm"]) == {"enabled", "provider"}
    assert body["llm"]["enabled"] is False
    assert body["llm"]["provider"] is None


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
