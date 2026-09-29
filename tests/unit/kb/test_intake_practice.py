"""Practice tests check hidden answers, durable replay, schedule and version pinning."""

import duckdb
import pytest

from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_maintenance import IntakeMaintenanceStore
from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_practice import IntakePracticeStore, verify_practice_export

SCOPES = {
    "knowledge:intake:read", "knowledge:intake:write",
    "namespace:research:read", "namespace:research:write",
}
CARDS = [{
    "kind": "explanation", "prompt": "Explain why the index was stale",
    "answer": "The subscription worker had stopped.",
    "mastery_criterion": "Explain the cause without notes on three separate reviews",
    "references": [{"kind": "concept", "id": "concept:index-worker",
                    "namespace": "research", "version": 2}],
}]


def test_corrected_intake_source_pauses_practice_until_card_revision():
    conn = duckdb.connect(":memory:")
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
    intake = IntakeStore(conn, now=lambda: 1_000)
    exploration = IntakeExplorationStore(conn, now=lambda: 1_000)
    session = intake.create("research", "Exploration", "source", intent="Capture", **kwargs)
    first = exploration.capture(
        "research", session["session_id"], "first", expected_revision=1,
        url="https://example.org/study", title="Study", content="Old conclusion",
        saved=True, **kwargs,
    )
    ref = first["references"][0]
    cards = [{**CARDS[0], "references": [ref]}]
    practice = IntakePracticeStore(conn, now=lambda: 1_000)
    pack = practice.create_pack("research", "source-pack", "Study", cards, **kwargs)
    review = practice.start_review("research", pack["pack_id"], "card-1", "old-review", **kwargs)
    assert practice.due("research", **kwargs)["cards"][0]["reviewable"]

    corrected = exploration.capture(
        "research", session["session_id"], "corrected", expected_revision=2,
        url="https://example.org/study", title="Corrected study", content="New conclusion",
        saved=True, **kwargs,
    )
    due = practice.due("research", **kwargs)["cards"][0]
    assert due["source_status"] == "superseded"
    assert not due["reviewable"]
    with pytest.raises(IntakeError) as stale:
        practice.start_review("research", pack["pack_id"], "card-1", "new-review", **kwargs)
    assert stale.value.code == "stale_practice_source"
    with pytest.raises(IntakeError) as paused:
        practice.command_review("research", review["review_id"], "attempt", 1, "attempt",
                                {"answer": "Old conclusion", "assisted": False}, **kwargs)
    assert paused.value.code == "stale_practice_source"
    findings = IntakeMaintenanceStore(conn, now=lambda: 1_000).scan("research", **kwargs)["findings"]
    assert any(item["reason"] == "superseded_practice_source" and
               item["target"]["id"] == pack["pack_id"] for item in findings)

    revised = practice.revise_pack(
        "research", pack["pack_id"], "updated", 1, "Study", [{**cards[0],
        "answer": "New conclusion", "references": [corrected["references"][-1]]}],
        list(practice.inspect_pack("research", pack["pack_id"], **kwargs)["interval_days"]),
        **kwargs,
    )
    assert revised["revision"] == 2
    assert practice.due("research", **kwargs)["cards"][0]["source_status"] == "current"
    assert practice.start_review("research", pack["pack_id"], "card-1", "new-review", **kwargs)["status"] == "active"


def test_practice_attempt_before_reveal_and_restart_replay(tmp_path):
    path = str(tmp_path / "practice.duckdb")
    store = IntakePracticeStore(duckdb.connect(path), now=lambda: 1_000)
    plugin_link = {
        "workspace_id": "personal", "account_id": "alice",
        "plugin_id": "flashcards-spaced-repetition", "collection": "decks",
        "record_id": "deck-55", "authoritative_version": 7,
        "representation": "intentional_snapshot", "authority": "modulo",
    }
    pack = store.create_pack(
        "research", "pack-key", "Index worker", CARDS,
        principal_id="alice", scopes=SCOPES, plugin_links=[plugin_link],
    )
    assert pack["plugin_links"] == [plugin_link]
    pack_id = pack["pack_id"]
    assert store.create_pack("research", "pack-key", "Index worker", CARDS,
                             principal_id="alice", scopes=SCOPES,
                             plugin_links=[plugin_link])["idempotent"]
    due = store.due("research", principal_id="alice", scopes=SCOPES)
    assert due["cards"][0]["card_id"] == "card-1"
    assert "answer" not in due["cards"][0]
    review = store.start_review(
        "research", pack_id, "card-1", "review-key",
        principal_id="alice", scopes=SCOPES,
    )
    review_id = review["review_id"]
    assert "answer" not in review
    with pytest.raises(IntakeError, match="answer follows"):
        store.command_review("research", review_id, "reveal-early", 1, "reveal", {},
                             principal_id="alice", scopes=SCOPES)
    attempt = store.command_review(
        "research", review_id, "attempt-key", 1, "attempt",
        {"answer": "The worker stopped", "assisted": False},
        principal_id="alice", scopes=SCOPES,
    )
    assert attempt["assistance"] == "reported_unaided"
    assert "answer" not in attempt
    store.conn.close()

    store = IntakePracticeStore(duckdb.connect(path), now=lambda: 2_000)
    assert store.command_review(
        "research", review_id, "attempt-key", 1, "attempt",
        {"answer": "The worker stopped", "assisted": False},
        principal_id="alice", scopes=SCOPES,
    )["idempotent"]
    with pytest.raises(IntakeError, match="identifies another action"):
        store.command_review("research", review_id, "attempt-key", 1, "attempt",
                             {"answer": "Different", "assisted": False},
                             principal_id="alice", scopes=SCOPES)
    revealed = store.command_review(
        "research", review_id, "reveal-key", 2, "reveal", {},
        principal_id="alice", scopes=SCOPES,
    )
    assert revealed["answer"] == CARDS[0]["answer"]
    assert revealed["answer_status"] == "author_supplied_unverified"
    assessed = store.command_review(
        "research", review_id, "assess-key", 3, "assess",
        {"passed": True, "notes": "I recalled the cause"},
        principal_id="alice", scopes=SCOPES,
    )
    assert assessed["status"] == "assessed"
    assert store.due("research", principal_id="alice", scopes=SCOPES)["cards"] == []
    assert store.inspect_review("research", review_id, revision=1,
                                principal_id="alice", scopes=SCOPES).get("answer") is None
    bundle = store.export_pack("research", pack_id, principal_id="alice", scopes=SCOPES)
    assert bundle["pack_revisions"][0]["plugin_links"] == [plugin_link]
    assert verify_practice_export(bundle) == {
        "valid": True, "pack_revision_count": 1, "review_count": 1,
    }
    assert len(bundle["reviews"][0]["revisions"]) == 4
    tampered = {**bundle, "current_revision": 2}
    assert verify_practice_export(tampered)["valid"] is False
    assert verify_practice_export({"contract": "noesis-intake-practice-export-v1",
                                   "sha256": "x", "bad": float("nan")})["valid"] is False
    with pytest.raises(IntakeError, match="owner"):
        store.inspect_review("research", review_id, principal_id="bob", scopes=SCOPES)
    store.conn.close()


def test_revision_resets_due_schedule_without_rewriting_historical_review():
    store = IntakePracticeStore(duckdb.connect(":memory:"), now=lambda: 1_000)
    pack = store.create_pack("research", "pack", "Worker", CARDS,
                             principal_id="alice", scopes=SCOPES)
    review = store.start_review("research", pack["pack_id"], "card-1", "review",
                                principal_id="alice", scopes=SCOPES)
    new_cards = [{**CARDS[0], "answer": "The indexing worker lost its lease."}]
    revised = store.revise_pack("research", pack["pack_id"], "edit", 1,
                                "Worker", new_cards, [1, 3, 10],
                                principal_id="alice", scopes=SCOPES)
    assert revised["revision"] == 2
    assert store.revise_pack("research", pack["pack_id"], "edit", 1,
                             "Worker", new_cards, [1, 3, 10],
                             principal_id="alice", scopes=SCOPES)["idempotent"]
    assert store.due("research", principal_id="alice", scopes=SCOPES)["cards"][0][
        "pack_revision"] == 2
    bundle = store.export_pack("research", pack["pack_id"],
                               principal_id="alice", scopes=SCOPES)
    assert verify_practice_export(bundle)["pack_revision_count"] == 2
    store.command_review("research", review["review_id"], "attempt", 1, "attempt",
                         {"answer": "Worker stopped", "assisted": True},
                         principal_id="alice", scopes=SCOPES)
    old = store.command_review("research", review["review_id"], "reveal", 2, "reveal", {},
                               principal_id="alice", scopes=SCOPES)
    assert old["answer"] == CARDS[0]["answer"]
    store.command_review("research", review["review_id"], "assess", 3, "assess",
                         {"passed": True, "notes": "Used a hint"},
                         principal_id="alice", scopes=SCOPES)
    assert store.due("research", principal_id="alice", scopes=SCOPES)["cards"][0][
        "pack_revision"] == 2
    store.conn.close()


def test_schedule_configuration_and_write_only_mutation_scope():
    store = IntakePracticeStore(duckdb.connect(":memory:"), now=lambda: 1_000)
    write_scopes = {"knowledge:intake:write", "namespace:research:write"}
    pack = store.create_pack("research", "write-only", "Worker", CARDS,
                             interval_days=[2, 5, 12],
                             principal_id="alice", scopes=write_scopes)
    review = store.start_review("research", pack["pack_id"], "card-1", "review",
                                principal_id="alice", scopes=write_scopes)
    assert "answer" not in review
    attempted = store.command_review(
        "research", review["review_id"], "attempt", 1, "attempt",
        {"answer": "The worker", "assisted": False},
        principal_id="alice", scopes=write_scopes,
    )
    assert attempted["status"] == "attempted"
    store.command_review("research", review["review_id"], "reveal", 2, "reveal", {},
                         principal_id="alice", scopes=write_scopes)
    store.command_review("research", review["review_id"], "assess", 3, "assess",
                         {"passed": True, "notes": "Recalled without notes"},
                         principal_id="alice", scopes=write_scopes)
    progress = store.conn.execute(
        "SELECT due_at_ms,stage FROM intake_practice_progress WHERE pack_id=?", [pack["pack_id"]]
    ).fetchone()
    assert progress == (1_000 + 2 * 86_400_000, 1)
    with pytest.raises(IntakeError, match="interval days"):
        store.create_pack("research", "bad", "Worker", CARDS,
                          interval_days=[3, 2], principal_id="alice", scopes=write_scopes)
    store.conn.close()


def test_pack_correction_resets_only_changed_cards_and_keeps_review_history():
    store = IntakePracticeStore(duckdb.connect(":memory:"), now=lambda: 1_000)
    cards = [CARDS[0], {**CARDS[0], "prompt": "What is the fallback?"}]
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
    pack = store.create_pack("research", "two-cards", "Worker", cards, **kwargs)
    review = store.start_review("research", pack["pack_id"], "card-1", "attempt-1", **kwargs)
    store.command_review("research", review["review_id"], "answer", 1, "attempt",
                         {"answer": "Worker", "assisted": False}, **kwargs)
    store.command_review("research", review["review_id"], "reveal", 2, "reveal", {}, **kwargs)
    store.command_review("research", review["review_id"], "assess", 3, "assess",
                         {"passed": True, "notes": "Recalled"}, **kwargs)
    before = store.conn.execute(
        "SELECT due_at_ms,stage,unassisted_passes FROM intake_practice_progress "
        "WHERE pack_id=? AND card_id='card-1'", [pack["pack_id"]],
    ).fetchone()
    revised = store.revise_pack(
        "research", pack["pack_id"], "correct-card-2", 1, "Worker",
        [cards[0], {**cards[1], "answer": "Use the repair guide."}], [1, 3, 7], **kwargs,
    )
    assert revised["revision"] == 2
    unchanged = store.conn.execute(
        "SELECT pack_revision,due_at_ms,stage,unassisted_passes FROM intake_practice_progress "
        "WHERE pack_id=? AND card_id='card-1'", [pack["pack_id"]],
    ).fetchone()
    changed = store.conn.execute(
        "SELECT pack_revision,due_at_ms,stage,unassisted_passes FROM intake_practice_progress "
        "WHERE pack_id=? AND card_id='card-2'", [pack["pack_id"]],
    ).fetchone()
    assert unchanged == (2, *before)
    assert changed == (2, 1_000, 0, 0)
    assert store.inspect_review("research", review["review_id"], **kwargs)["answer"] == CARDS[0]["answer"]


def test_corrected_or_withdrawn_document_pauses_practice_without_erasing_history():
    from src.ingestion.revisions import DocumentRevisionStore

    conn = duckdb.connect(":memory:")
    scopes = SCOPES | {"document:doc-guide:read"}
    kwargs = {"principal_id": "alice", "scopes": scopes}
    documents = DocumentRevisionStore(conn)
    first = documents.observe({"document_id": "doc-guide", "content": "Restart the worker.", "metadata": {}},
                              committed_watermark=1)
    ref = {"kind": "document", "id": "doc-guide", "namespace": "research", "version": first["revision"]}
    practice = IntakePracticeStore(conn, now=lambda: 1_000)
    pack = practice.create_pack("research", "doc-pack", "Guide", [{**CARDS[0], "references": [ref]}], **kwargs)
    review = practice.start_review("research", pack["pack_id"], "card-1", "before", **kwargs)
    assert practice.due("research", **kwargs)["cards"][0]["source_status"] == "current"
    # Without document read scope the source cannot be assessed.
    assert practice.due("research", principal_id="alice", scopes=SCOPES)["cards"][0]["source_status"] == "unavailable"

    documents.observe({"document_id": "doc-guide", "content": "Drain the queue, then restart.", "metadata": {}},
                      committed_watermark=2)

    due = practice.due("research", **kwargs)["cards"][0]
    assert due["source_status"] == "superseded" and not due["reviewable"]
    assert practice.inspect_review("research", review["review_id"], **kwargs)["review_id"] == review["review_id"]
    findings = IntakeMaintenanceStore(conn, now=lambda: 1_000).scan("research", **kwargs)["findings"]
    assert any(item["reason"] == "superseded_practice_source" for item in findings)
