"""Treaty participants and treaties matched across sources through reviewable identity (#2610)."""

from __future__ import annotations

import pytest

from src.kb.treaties_identity import TreatiesIdentity
from src.kb.treaties_records import TreatiesError
from tests.unit import treaties_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn)
    places = h.seed_places(conn)
    identity = TreatiesIdentity(conn, now=h.Clock())
    yield conn, places, identity
    conn.close()


def test_candidates_carry_method_evidence_and_confidence_and_nothing_is_accepted(env):
    _conn, places, identity = env
    result = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    places_by_subject = {c["subject"]: c for c in result["candidates"] if c["kind"] == "participant-place"}
    germany = places_by_subject["treaties:participant:coe:germany"]
    assert germany["target"] == f"place:{places['DE']}" and germany["method"] == "exact-name-coded-place"
    assert germany["confidence"] == 0.5 and germany["state"] == "proposed"
    assert germany["evidence"]["place"]["codes"] == {"iso3166-1-alpha2": "DE", "iso3166-1-alpha3": "DEU"}
    assert all(c["state"] == "proposed" for c in result["candidates"])
    (treaty,) = [c for c in result["candidates"] if c["kind"] == "treaty"]
    assert (treaty["subject"], treaty["target"]) == (h.CELLAR_TREATY, h.COE_TREATY)
    assert treaty["method"] == "published-cross-reference"
    assert treaty["evidence"]["cross_references"][0]["as_written"] == "CETS No. 990"
    again = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert again["created"] == []  # idempotent


def test_unmatched_participants_and_treaties_stay_visible(env):
    _conn, _places, identity = env
    result = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    unmatched = {u["subject"] for u in result["unmatched"]}
    assert "treaties:participant:untc:examplestan" in unmatched  # the fixture place carries no ISO code
    assert "treaties:participant:coe:european-union" in unmatched
    assert h.UNTC_TREATY in unmatched  # no other source publishes a shared identifier


def test_published_codes_come_before_names(env):
    conn, _places, identity = env
    conn.execute("UPDATE treaty_action_revisions SET record_json=replace(record_json, "
                 "'\"published_codes\":[]', '\"published_codes\":[{\"scheme\":\"op-country\",\"value\":\"FRA\"}]') "
                 "WHERE participant_key='treaties:participant:coe:france'")
    result = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    france = next(c for c in result["candidates"] if c["subject"] == "treaties:participant:coe:france")
    assert france["method"] == "published-code" and france["confidence"] == 0.95
    assert france["evidence"]["codes_compared"] == ["iso3166-1-alpha3:FRA"]


def test_review_accept_reject_and_revert_are_entity_history_decisions(env):
    conn, _places, identity = env
    result = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    germany = next(c for c in result["candidates"] if c["subject"] == "treaties:participant:untc:germany")
    with pytest.raises(TreatiesError):
        identity.review(h.NS, germany["candidate_id"], "accept", "ok", principal_id="bob", scopes=h.SCOPES)
    accepted = identity.review(h.NS, germany["candidate_id"], "accept", "ISO code on the place", principal_id="bob",
                               scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "bob" and accepted["decision_id"]
    decision = conn.execute("SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?",
                            [accepted["decision_id"]]).fetchone()
    assert decision == ("match",)
    assert [p["participant_key"] for p in identity.participants_for_place(h.NS, "iso3166:DE")] == [
        "treaties:participant:untc:germany"]
    reverted = identity.revert(h.NS, germany["candidate_id"], "wrong place revision", principal_id="bob",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and identity.participants_for_place(h.NS, "iso3166:DE") == []
    assert [s["state"] for s in reverted["history"]] == ["proposed", "accepted", "reverted"]
    treaty = next(c for c in result["candidates"] if c["kind"] == "treaty")
    rejected = identity.review(h.NS, treaty["candidate_id"], "reject", "different instrument", principal_id="bob",
                               scopes=h.REVIEW_SCOPES)
    assert rejected["state"] == "rejected" and identity.related_treaties(h.NS, h.COE_TREATY) == []


def test_a_name_alone_is_never_accepted(env):
    conn, _places, identity = env
    conn.execute("CREATE TABLE IF NOT EXISTS canonical_entities (canonical_id TEXT, preferred_name TEXT, "
                 "entity_type TEXT)")
    conn.execute("INSERT INTO canonical_entities VALUES ('ent-eu', 'European Union', 'organization')")
    result = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    entity = next(c for c in result["candidates"] if c["kind"] == "participant-entity")
    assert entity["method"] == "name-only" and entity["confidence"] == 0.1
    with pytest.raises(TreatiesError) as error:
        identity.review(h.NS, entity["candidate_id"], "accept", "same name", principal_id="bob",
                        scopes=h.REVIEW_SCOPES)
    assert error.value.code == "insufficient_evidence"
