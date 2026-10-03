"""AI06 (#2776, track #2742): cross-source identity is proposed on stated identifiers, reviewed and revertible."""

from __future__ import annotations

import pytest

from src.kb.ai_models_identity import AiModelsIdentity
from src.kb.ai_models_records import AiModelsError
from src.kb.entity_history import EntityHistoryStore
from tests.unit import ai_models_harness as h


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.load_all(conn)
    return conn, AiModelsIdentity(conn)


def _pair(identity, left_kind, left, right_kind, right):
    left_id = h.record(identity.conn, left_kind, left)["record_id"]
    right_id = h.record(identity.conn, right_kind, right)["record_id"]
    return next(m for m in identity.matches(h.NS, scopes=h.SCOPES)
                if m["left_record_id"] == left_id and m["right_record_id"] == right_id)


def test_matches_rest_on_stated_identifiers_with_method_evidence_and_confidence(loaded):
    conn, identity = loaded
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert len(proposed["created"]) == 3 and all(m["state"] == "proposed" for m in proposed["matches"])
    epoch = _pair(identity, "epoch-model", "Fixture Model", "hub-model", h.MODEL)
    assert epoch["method"] == "stated-repository-id" and epoch["confidence"] == "high"
    assert epoch["evidence"]["methods"] == [{"method": "stated-repository-id", "identifier": h.MODEL,
                                             "stated_by": "epoch-ai (row link)"}]
    assert epoch["left_revision_id"] and epoch["right_revision_id"] and epoch["merged"] is False
    corpus = _pair(identity, "hub-dataset", h.CORPUS, "openml-dataset", "990061")
    assert [m["method"] for m in corpus["evidence"]["methods"]] == [
        "stated-repository-id", "stated-openml-id", "shared-doi"]
    assert corpus["confidence"] == "high"
    version_two = _pair(identity, "hub-dataset", h.CORPUS, "openml-dataset", "990062")
    assert "stated-openml-id" not in {m["method"] for m in version_two["evidence"]["methods"]}
    # Stated identifiers come before names, and a shared name alone is never a match.
    small_epoch = h.record(conn, "epoch-model", "Fixture Small Model")["record_id"]
    small_hub = h.record(conn, "hub-model", h.SMALL)["record_id"]
    assert not identity.matches(h.NS, scopes=h.SCOPES, record_id=small_epoch)
    assert not identity.matches(h.NS, scopes=h.SCOPES, record_id=small_hub)
    unmatched = {u["record_id"]: u for u in identity.unmatched(h.NS, scopes=h.SCOPES)}
    assert unmatched[small_epoch]["reason"] == "no accepted match; a shared name is never a match"
    assert small_hub in unmatched and h.record(conn, "hub-model", h.MODEL)["record_id"] in unmatched
    again = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert again["created"] == []  # idempotent


def test_review_and_revert_are_entity_identity_decisions_and_nothing_is_auto_merged(loaded):
    conn, identity = loaded
    identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    epoch = _pair(identity, "epoch-model", "Fixture Model", "hub-model", h.MODEL)
    with pytest.raises(AiModelsError) as caught:
        identity.review(h.NS, epoch["match_id"], "accept", "same model", principal_id="alice", scopes=h.SCOPES)
    assert caught.value.code == "self_review"
    with pytest.raises(AiModelsError):
        identity.review(h.NS, epoch["match_id"], "accept", "x", principal_id="bob", scopes=h.READ_ONLY)
    accepted = identity.review(h.NS, epoch["match_id"], "accept", "the Epoch row links the repository",
                               principal_id="bob", scopes=h.SCOPES)
    assert accepted["state"] == "accepted" and accepted["decision_id"].startswith("entity-decision:")
    assert [s["state"] for s in accepted["history"]] == ["proposed", "accepted"]
    decision = conn.execute("SELECT decision_type, payload_json FROM entity_identity_decisions WHERE decision_id=?",
                            [accepted["decision_id"]]).fetchone()
    assert decision[0] == "match" and '"merge":false' in decision[1]
    hub_id = h.record(conn, "hub-model", h.MODEL)["record_id"]
    epoch_id = h.record(conn, "epoch-model", "Fixture Model")["record_id"]
    assert [c["record_id"] for c in identity.counterparts(h.NS, hub_id)] == [epoch_id]
    # Both records stay separate with their own revisions.
    assert identity.store.record(h.NS, hub_id)["revision_count"] == 2
    history = EntityHistoryStore(conn, initialize=False)
    entity = "ent-ai-" + hub_id.replace(":", "-")
    assert history.resolve(h.NS, entity, scopes={"knowledge:entity-history:read"})["canonical_id"] == entity
    reverted = identity.revert(h.NS, epoch["match_id"], "evidence withdrawn", principal_id="carol",
                               scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and identity.counterparts(h.NS, hub_id) == []
    assert hub_id in {u["record_id"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)}
    corpus = _pair(identity, "hub-dataset", h.CORPUS, "openml-dataset", "990062")
    rejected = identity.review(h.NS, corpus["match_id"], "reject", "another dataset version", principal_id="bob",
                               scopes=h.SCOPES)
    assert rejected["state"] == "rejected"
    with pytest.raises(AiModelsError):
        identity.review(h.NS, corpus["match_id"], "accept", "again", principal_id="carol", scopes=h.SCOPES)
