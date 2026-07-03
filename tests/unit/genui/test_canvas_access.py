"""M8.3: the canvas access model and the full save/reopen/share acceptance.

Asserts the permission matrix, that the store enforces it, and that the M8
acceptance harness reports the whole save -> reopen -> share flow green."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.genui import canvas_access, canvas_store

duckdb = pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[3]


def _load_harness():
    path = REPO / "scripts/genui/m8_acceptance.py"
    spec = importlib.util.spec_from_file_location("m8_acceptance_mod", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _now():
    return datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _spec(topic="climate policy"):
    return {
        "spec_version": "ui-spec-v1",
        "intent": "track the debate",
        "title": "Climate policy",
        "subtitle": "",
        "generated_by": "heuristic",
        "facets": ["claims"],
        "topic": topic,
        "source_type": None,
        "panels": [
            {"id": "p1", "type": "claims", "title": "Key claims", "span": 6,
             "priority": 0.8, "rationale": "", "endpoint": None,
             "params": {"topic": topic}, "body": ""}
        ],
    }


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    canvas_store.ensure_schema(c)
    yield c
    c.close()


# --- the declarative model -----------------------------------------------------


def test_permission_matrix_matches_the_stated_model():
    A = canvas_access
    # Owner: everything. Viewer: read only. None: nothing.
    assert all(A.can(A.ROLE_OWNER, act) for act in A.ACTIONS)
    assert A.can(A.ROLE_VIEWER, A.READ)
    assert not any(A.can(A.ROLE_VIEWER, act) for act in (A.WRITE, A.SHARE, A.DELETE))
    assert not any(A.can(A.ROLE_NONE, act) for act in A.ACTIONS)


def test_role_resolution():
    A = canvas_access
    canvas = {"owner": "alice"}
    assert A.role_for(canvas, "alice") == A.ROLE_OWNER
    assert A.role_for(canvas, "bob") == A.ROLE_NONE
    # A share link grants a non-owner the viewer role, never more.
    assert A.role_for(canvas, "bob", via_share_token=True) == A.ROLE_VIEWER
    # Ownership always wins over the share flag.
    assert A.role_for(canvas, "alice", via_share_token=True) == A.ROLE_OWNER


def test_authorize_is_the_single_decision():
    A = canvas_access
    canvas = {"owner": "alice"}
    assert A.authorize(canvas, "alice", A.WRITE) is True
    assert A.authorize(canvas, "bob", A.READ) is False
    assert A.authorize(canvas, "bob", A.READ, via_share_token=True) is True
    assert A.authorize(canvas, "bob", A.WRITE, via_share_token=True) is False


# --- the model enforced through the store -------------------------------------


def test_store_enforces_owner_only_writes_and_deletes(conn):
    saved = canvas_store.save_canvas(conn, owner="alice", spec=_spec(), now=_now())
    cid = saved["id"]
    # None-role (bob) can neither read, delete, nor share.
    assert canvas_store.get_canvas(conn, cid, owner="bob") is None
    assert canvas_store.delete_canvas(conn, cid, owner="bob") is False
    assert canvas_store.share_canvas(conn, cid, owner="bob") is None
    # Owner can do all three.
    assert canvas_store.get_canvas(conn, cid, owner="alice") is not None
    assert canvas_store.share_canvas(conn, cid, owner="alice")
    assert canvas_store.delete_canvas(conn, cid, owner="alice") is True


# --- the full acceptance flow --------------------------------------------------


def test_full_save_reopen_share_acceptance_flow(conn):
    # 1) save with live bindings; 2) reopen intact
    bindings = {"p1": {"tool": "list_claims"}}
    saved = canvas_store.save_canvas(
        conn, owner="alice", spec=_spec(), now=_now(), data_bindings=bindings
    )
    cid = saved["id"]
    reopened = canvas_store.get_canvas(conn, cid, owner="alice")
    assert reopened["spec"] == _spec() and reopened["data_bindings"] == bindings

    # 3) share; 4) viewer sees read-only, no owner leaked
    token = canvas_store.share_canvas(conn, cid, owner="alice")
    shared = canvas_store.get_shared_canvas(conn, token)
    assert shared["read_only"] is True and "owner" not in shared

    # 5) second user cannot edit the original -- his write is a copy
    bob_copy = canvas_store.save_canvas(
        conn, owner="bob", spec=_spec("hijacked"), now=_now(), canvas_id=cid
    )
    assert bob_copy["id"] != cid
    assert canvas_store.get_canvas(conn, cid)["spec"]["topic"] == "climate policy"

    # 6) revoke stops the link resolving
    canvas_store.unshare_canvas(conn, cid, owner="alice")
    assert canvas_store.get_shared_canvas(conn, token) is None


def test_m8_acceptance_harness_reports_green():
    result = _load_harness().main()
    assert result["ok"] is True
    assert result["save_reopen_ok"] and result["viewer_read_only_ok"]
    assert result["access_enforced"] and result["matrix_ok"] and result["revoked_ok"]
