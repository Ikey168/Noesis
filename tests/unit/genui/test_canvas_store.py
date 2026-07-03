"""M8.1: the server-persisted canvas store. A canvas saves and reopens with its
spec and live data bindings intact, is scoped to its owner, and updates in place
when re-saved."""

from datetime import datetime, timezone

import pytest

from src.genui import canvas_store

duckdb = pytest.importorskip("duckdb")


def _now():
    return datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _spec(topic="climate policy"):
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
    # Per-panel live data bindings: which data-mode tool feeds each panel, plus a
    # snapshot the client captured, so reopening rebinds to live data.
    return {
        "p1": {
            "server": "neuronews-arguments",
            "tool": "list_claims",
            "arguments": {"topic": "climate policy"},
            "snapshot": {"claims": [{"claim_id": "k1", "text": "a claim"}]},
        }
    }


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    canvas_store.ensure_schema(c)
    yield c
    c.close()


def test_save_then_reopen_preserves_spec_and_data_bindings(conn):
    saved = canvas_store.save_canvas(
        conn, owner="alice", spec=_spec(), now=_now(), data_bindings=_bindings()
    )
    assert saved["id"]
    assert saved["owner"] == "alice"

    reopened = canvas_store.get_canvas(conn, saved["id"])
    assert reopened is not None
    # The spec round-trips byte-for-byte (structurally).
    assert reopened["spec"] == _spec()
    # The live data bindings survive intact -- the whole point of M8.1.
    assert reopened["data_bindings"] == _bindings()
    assert reopened["data_bindings"]["p1"]["tool"] == "list_claims"


def test_reopen_is_scoped_to_owner(conn):
    saved = canvas_store.save_canvas(conn, owner="alice", spec=_spec(), now=_now())
    # Bob cannot reopen Alice's canvas by id.
    assert canvas_store.get_canvas(conn, saved["id"], owner="bob") is None
    # Alice can.
    assert canvas_store.get_canvas(conn, saved["id"], owner="alice") is not None


def test_resave_with_id_updates_in_place(conn):
    saved = canvas_store.save_canvas(conn, owner="alice", spec=_spec("energy"), now=_now())
    updated = canvas_store.save_canvas(
        conn,
        owner="alice",
        spec=_spec("energy transition"),
        now=datetime(2026, 7, 3, 13, 0, 0, tzinfo=timezone.utc),
        canvas_id=saved["id"],
    )
    assert updated["id"] == saved["id"]  # same canvas, not a duplicate
    assert updated["spec"]["topic"] == "energy transition"
    assert len(canvas_store.list_canvases(conn, "alice")) == 1
    # created_at is preserved across the update.
    assert updated["created_at"] == saved["created_at"]


def test_resave_with_foreign_id_creates_new_canvas(conn):
    alice = canvas_store.save_canvas(conn, owner="alice", spec=_spec(), now=_now())
    # Bob supplying Alice's id must not overwrite it -- he gets a new canvas.
    bob = canvas_store.save_canvas(
        conn, owner="bob", spec=_spec("markets"), now=_now(), canvas_id=alice["id"]
    )
    assert bob["id"] != alice["id"]
    assert canvas_store.get_canvas(conn, alice["id"])["owner"] == "alice"


def test_list_is_owner_scoped_and_summarised(conn):
    canvas_store.save_canvas(conn, owner="alice", spec=_spec("a"), now=_now())
    canvas_store.save_canvas(conn, owner="alice", spec=_spec("b"), now=_now())
    canvas_store.save_canvas(conn, owner="bob", spec=_spec("c"), now=_now())

    alice_list = canvas_store.list_canvases(conn, "alice")
    assert len(alice_list) == 2
    assert all("spec" not in entry for entry in alice_list)  # summaries only
    assert all(entry["panel_count"] == 1 for entry in alice_list)
    assert len(canvas_store.list_canvases(conn, "bob")) == 1


def test_delete_is_owner_scoped(conn):
    saved = canvas_store.save_canvas(conn, owner="alice", spec=_spec(), now=_now())
    assert canvas_store.delete_canvas(conn, saved["id"], "bob") is False  # not bob's
    assert canvas_store.delete_canvas(conn, saved["id"], "alice") is True
    assert canvas_store.get_canvas(conn, saved["id"]) is None


def test_oversize_canvas_is_refused(conn):
    big = _spec()
    big["panels"][0]["body"] = "x" * (canvas_store.MAX_CANVAS_BYTES + 1)
    with pytest.raises(canvas_store.CanvasStoreError):
        canvas_store.save_canvas(conn, owner="alice", spec=big, now=_now())
