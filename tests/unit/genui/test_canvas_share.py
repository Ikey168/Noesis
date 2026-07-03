"""M8.2: read-only canvas sharing at the store layer. An owner mints a stable
share token; a viewer resolves the canvas read-only by that token without the
owner's identity; revoking the token stops it resolving."""

from datetime import datetime, timezone

import pytest

from src.genui import canvas_store

duckdb = pytest.importorskip("duckdb")


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


def test_share_mints_a_stable_token_owner_only(conn):
    saved = canvas_store.save_canvas(conn, owner="alice", spec=_spec(), now=_now())
    token = canvas_store.share_canvas(conn, saved["id"], owner="alice")
    assert token
    # Idempotent: sharing again returns the same token, so the link stays stable.
    assert canvas_store.share_canvas(conn, saved["id"], owner="alice") == token
    # A non-owner cannot mint a share token.
    assert canvas_store.share_canvas(conn, saved["id"], owner="bob") is None


def test_viewer_resolves_shared_canvas_read_only(conn):
    saved = canvas_store.save_canvas(
        conn, owner="alice", spec=_spec(),
        now=_now(), data_bindings={"p1": {"tool": "list_claims"}},
    )
    token = canvas_store.share_canvas(conn, saved["id"], owner="alice")

    shared = canvas_store.get_shared_canvas(conn, token)
    assert shared is not None
    assert shared["read_only"] is True
    assert shared["spec"] == _spec()
    assert shared["data_bindings"] == {"p1": {"tool": "list_claims"}}
    # The owner's identity is never exposed through the share view.
    assert "owner" not in shared


def test_unknown_token_does_not_resolve(conn):
    assert canvas_store.get_shared_canvas(conn, "nope") is None


def test_revoking_a_share_stops_it_resolving(conn):
    saved = canvas_store.save_canvas(conn, owner="alice", spec=_spec(), now=_now())
    token = canvas_store.share_canvas(conn, saved["id"], owner="alice")
    assert canvas_store.get_shared_canvas(conn, token) is not None

    assert canvas_store.unshare_canvas(conn, saved["id"], owner="alice") is True
    assert canvas_store.get_shared_canvas(conn, token) is None
    # A non-owner cannot revoke.
    assert canvas_store.unshare_canvas(conn, saved["id"], owner="bob") is False


def test_update_in_place_preserves_the_share_token(conn):
    saved = canvas_store.save_canvas(conn, owner="alice", spec=_spec("a"), now=_now())
    token = canvas_store.share_canvas(conn, saved["id"], owner="alice")
    canvas_store.save_canvas(
        conn, owner="alice", spec=_spec("b"), now=_now(), canvas_id=saved["id"]
    )
    # Editing the canvas does not break its existing share link.
    shared = canvas_store.get_shared_canvas(conn, token)
    assert shared is not None
    assert shared["spec"]["topic"] == "b"
