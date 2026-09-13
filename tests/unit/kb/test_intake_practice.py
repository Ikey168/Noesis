"""Practice tests check hidden answers, durable replay, schedule and version pinning."""

import duckdb
import pytest

from src.kb.intake_modes import IntakeError
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


def test_practice_attempt_before_reveal_and_restart_replay(tmp_path):
    path = str(tmp_path / "practice.duckdb")
    store = IntakePracticeStore(duckdb.connect(path), now=lambda: 1_000)
    pack = store.create_pack(
        "research", "pack-key", "Index worker", CARDS,
        principal_id="alice", scopes=SCOPES,
    )
    pack_id = pack["pack_id"]
    assert store.create_pack("research", "pack-key", "Index worker", CARDS,
                             principal_id="alice", scopes=SCOPES)["idempotent"]
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
