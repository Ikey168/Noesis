"""Route test for POST /api/v1/ui/refine (M6 in-canvas refinement).

Loads the genui route module BY PATH and drives the refine endpoint, which turns
a natural-language instruction into a spec diff and applies it in place.
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
def client():
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def _spec_dict():
    return {
        "spec_version": "ui-spec-v1",
        "intent": "overview of the debate",
        "title": "Overview",
        "subtitle": "",
        "generated_by": "heuristic",
        "facets": [],
        "topic": None,
        "source_type": None,
        "panels": [
            {"id": "n", "type": "note", "title": "Plan", "span": 12, "priority": 1.0,
             "rationale": "", "endpoint": None, "params": {}, "body": "how it was built"},
            {"id": "p1", "type": "claims", "title": "Extracted claims", "span": 6,
             "priority": 0.8, "rationale": "", "endpoint": None, "params": {"topic": "x"}, "body": ""},
            {"id": "p2", "type": "articles", "title": "Latest documents", "span": 6,
             "priority": 0.7, "rationale": "", "endpoint": None, "params": {}, "body": ""},
        ],
    }


def test_refine_removes_a_panel(client):
    resp = client.post("/api/v1/ui/refine", json={"spec": _spec_dict(), "instruction": "remove claims"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    types = {p["type"] for p in body["spec"]["panels"]}
    assert "claims" not in types
    assert any(op["op"] == "remove" for op in body["diff"])


def test_refine_focus_retargets_data_panels(client):
    resp = client.post(
        "/api/v1/ui/refine",
        json={"spec": _spec_dict(), "instruction": "focus on climate policy"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    claims = next(p for p in body["spec"]["panels"] if p["type"] == "claims")
    assert claims["params"]["topic"] == "climate policy"


def test_unrecognized_instruction_is_a_noop(client):
    resp = client.post("/api/v1/ui/refine", json={"spec": _spec_dict(), "instruction": "make it nicer"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is False
    assert body["diff"] == []
    assert body["spec"] == _spec_dict()  # unchanged


def test_invalid_spec_is_rejected(client):
    bad = _spec_dict()
    bad["panels"] = []
    resp = client.post("/api/v1/ui/refine", json={"spec": bad, "instruction": "remove claims"})
    assert resp.status_code == 400
