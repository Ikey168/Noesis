"""Route tests for src/api/routes/canvas_routes.py (M8.1 persisted canvas).

Loads the route module BY PATH (never via src.api.routes, whose __init__
eagerly imports heavy ML modules), mounts only that router, and points its
warehouse connection at an in-memory DuckDB so no real warehouse is touched.
Covers the save -> reopen round-trip (spec + live data bindings intact), owner
scoping, spec validation, and list/delete.
"""

import importlib.util
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

pytest.importorskip("fastapi")
duckdb = pytest.importorskip("duckdb")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "canvas_routes_under_test", REPO / "src/api/routes/canvas_routes.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _valid_spec(topic="climate policy"):
    return {
        "spec_version": "ui-spec-v1",
        "intent": "track the climate policy debate",
        "title": "Climate policy",
        "subtitle": "",
        "generated_by": "heuristic",
        "facets": ["claims"],
        "topic": topic,
        "source_type": None,
        "panels": [
            {
                "id": "p1",
                "type": "claims",
                "title": "Key claims",
                "span": 6,
                "priority": 0.8,
                "rationale": "",
                "endpoint": None,
                "params": {"topic": topic},
                "body": "",
            }
        ],
    }


def _bindings():
    return {
        "p1": {
            "server": "neuronews-arguments",
            "tool": "list_claims",
            "arguments": {"topic": "climate policy"},
            "snapshot": {"claims": [{"claim_id": "k1"}]},
        }
    }


@pytest.fixture
def client(monkeypatch):
    conn = duckdb.connect(":memory:")
    lock = threading.Lock()
    monkeypatch.setattr(mod, "_conn", lambda: (conn, lock))
    app = FastAPI()
    app.include_router(mod.router)
    yield TestClient(app)
    conn.close()


def test_save_then_reopen_round_trips_spec_and_bindings(client):
    save = client.post(
        "/api/v1/ui/canvas",
        json={"spec": _valid_spec(), "data_bindings": _bindings()},
        headers={"X-Canvas-Owner": "alice"},
    )
    assert save.status_code == 200
    cid = save.json()["canvas"]["id"]
    assert cid

    reopen = client.get(f"/api/v1/ui/canvas/{cid}", headers={"X-Canvas-Owner": "alice"})
    assert reopen.status_code == 200
    canvas = reopen.json()["canvas"]
    assert canvas["spec"] == _valid_spec()
    assert canvas["data_bindings"] == _bindings()  # live bindings survive


def test_reopen_is_owner_scoped(client):
    cid = client.post(
        "/api/v1/ui/canvas", json={"spec": _valid_spec()}, headers={"X-Canvas-Owner": "alice"}
    ).json()["canvas"]["id"]
    # A different owner gets a 404, not another user's canvas.
    assert client.get(f"/api/v1/ui/canvas/{cid}", headers={"X-Canvas-Owner": "bob"}).status_code == 404


def test_invalid_spec_is_rejected(client):
    bad = _valid_spec()
    bad["panels"] = []  # a spec with no panels is invalid
    resp = client.post("/api/v1/ui/canvas", json={"spec": bad}, headers={"X-Canvas-Owner": "alice"})
    assert resp.status_code == 400


def test_list_and_delete(client):
    for topic in ("a", "b"):
        client.post(
            "/api/v1/ui/canvas", json={"spec": _valid_spec(topic)}, headers={"X-Canvas-Owner": "alice"}
        )
    listed = client.get("/api/v1/ui/canvas", headers={"X-Canvas-Owner": "alice"}).json()
    assert listed["count"] == 2

    cid = listed["canvases"][0]["id"]
    assert client.delete(f"/api/v1/ui/canvas/{cid}", headers={"X-Canvas-Owner": "alice"}).status_code == 200
    assert client.get("/api/v1/ui/canvas", headers={"X-Canvas-Owner": "alice"}).json()["count"] == 1


def test_update_in_place_with_id(client):
    cid = client.post(
        "/api/v1/ui/canvas", json={"spec": _valid_spec("energy")}, headers={"X-Canvas-Owner": "alice"}
    ).json()["canvas"]["id"]
    client.post(
        "/api/v1/ui/canvas",
        json={"spec": _valid_spec("energy transition"), "id": cid},
        headers={"X-Canvas-Owner": "alice"},
    )
    listed = client.get("/api/v1/ui/canvas", headers={"X-Canvas-Owner": "alice"}).json()
    assert listed["count"] == 1  # updated, not duplicated
    canvas = client.get(f"/api/v1/ui/canvas/{cid}", headers={"X-Canvas-Owner": "alice"}).json()["canvas"]
    assert canvas["spec"]["topic"] == "energy transition"


# --- M8.2: read-only sharing through the routes --------------------------------


def _own(cid_owner):
    return {"X-Canvas-Owner": cid_owner}


def test_share_link_renders_for_a_second_user_read_only(client):
    cid = client.post(
        "/api/v1/ui/canvas", json={"spec": _valid_spec(), "data_bindings": _bindings()},
        headers=_own("alice"),
    ).json()["canvas"]["id"]

    share = client.post(f"/api/v1/ui/canvas/{cid}/share", headers=_own("alice"))
    assert share.status_code == 200
    token = share.json()["share_token"]
    assert token and share.json()["url"].endswith(f"/canvas/shared/{token}")

    # A second user (different owner) opens the link and gets the canvas read-only,
    # with its live data bindings, and no owner identity leaked.
    viewed = client.get(f"/api/v1/ui/canvas/shared/{token}", headers=_own("bob"))
    assert viewed.status_code == 200
    canvas = viewed.json()["canvas"]
    assert canvas["read_only"] is True
    assert canvas["spec"] == _valid_spec()
    assert canvas["data_bindings"] == _bindings()
    assert "owner" not in canvas


def test_second_user_cannot_edit_the_shared_canvas(client):
    cid = client.post(
        "/api/v1/ui/canvas", json={"spec": _valid_spec("original")}, headers=_own("alice")
    ).json()["canvas"]["id"]
    client.post(f"/api/v1/ui/canvas/{cid}/share", headers=_own("alice"))

    # Bob cannot reopen it by id (owner-scoped) ...
    assert client.get(f"/api/v1/ui/canvas/{cid}", headers=_own("bob")).status_code == 404
    # ... and POSTing with Alice's id under Bob's owner makes Bob a *copy*, never
    # mutating Alice's canvas.
    bob_id = client.post(
        "/api/v1/ui/canvas", json={"spec": _valid_spec("hijacked"), "id": cid}, headers=_own("bob")
    ).json()["canvas"]["id"]
    assert bob_id != cid
    alice_view = client.get(f"/api/v1/ui/canvas/{cid}", headers=_own("alice")).json()["canvas"]
    assert alice_view["spec"]["topic"] == "original"  # untouched


def test_revoked_link_stops_resolving(client):
    cid = client.post(
        "/api/v1/ui/canvas", json={"spec": _valid_spec()}, headers=_own("alice")
    ).json()["canvas"]["id"]
    token = client.post(f"/api/v1/ui/canvas/{cid}/share", headers=_own("alice")).json()["share_token"]
    assert client.get(f"/api/v1/ui/canvas/shared/{token}").status_code == 200

    revoke = client.delete(f"/api/v1/ui/canvas/{cid}/share", headers=_own("alice"))
    assert revoke.status_code == 200
    assert client.get(f"/api/v1/ui/canvas/shared/{token}").status_code == 404


def test_non_owner_cannot_mint_a_share_link(client):
    cid = client.post(
        "/api/v1/ui/canvas", json={"spec": _valid_spec()}, headers=_own("alice")
    ).json()["canvas"]["id"]
    assert client.post(f"/api/v1/ui/canvas/{cid}/share", headers=_own("bob")).status_code == 404
