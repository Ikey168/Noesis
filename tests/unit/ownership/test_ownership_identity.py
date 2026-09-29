"""Reviewable, reversible identity reconciliation across LEI, register numbers and OpenCorporates (#1857)."""

import pytest

from src.kb.entity_history import EntityHistoryStore
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_store import OwnershipError, OwnershipStore
from tests.unit.ownership import harness
from tests.unit.ownership.harness import HOLD_KEYS, INT_KEYS, NS, SCOPES, UK_KEYS, candidate

HISTORY = {"knowledge:entity-history:read"}


@pytest.fixture()
def proposed():
    env = harness.Env().ready()
    market = harness.seed_market(env.conn)
    service = OwnershipIdentityService(env.conn, now=env.now)
    result = service.propose(NS, principal_id=harness.PRINCIPAL, scopes=SCOPES, market=market, lei_namespace=NS)
    return env, service, result


def test_candidates_carry_basis_evidence_and_confidence(proposed):
    _, service, result = proposed
    bases = {(c["left_key"], c["right_key"]): (c["basis"], c["confidence"]) for c in result["candidates"]}
    assert bases[tuple(sorted((UK_KEYS["gleif"], UK_KEYS["bods"])))] == ("exact-identifier", 0.95)
    assert bases[tuple(sorted((UK_KEYS["ch"], UK_KEYS["bods"])))] == ("exact-identifier", 0.95)
    assert bases[tuple(sorted((UK_KEYS["gleif"], UK_KEYS["ch"])))] == ("cross-referenced-identifier", 0.8)
    assert bases[tuple(sorted((INT_KEYS["gleif"], INT_KEYS["ch_psc"])))] == ("name-jurisdiction", 0.35)
    via_market = candidate(service, HOLD_KEYS["gleif"], HOLD_KEYS["sec"])
    assert via_market["basis"] == "cross-referenced-identifier"
    assert via_market["evidence"][0]["via"] == "market instrument master"  # CIK -> LEI from the instrument master
    assert all(c["state"] == "proposed" and c["decision_id"] is None for c in result["candidates"])
    assert "never merged" in result["candidates"][0]["notice"]
    again = service.propose(NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert again["proposed"] == []  # idempotent


def test_accepting_is_an_entity_history_decision_not_a_merge(proposed):
    env, service, _ = proposed
    before = env.conn.execute("SELECT count(*) FROM ownership_record_revisions").fetchone()[0]
    target = candidate(service, UK_KEYS["gleif"], UK_KEYS["ch"])
    with pytest.raises(OwnershipError):
        service.review(NS, target["candidate_id"], "accept", "same number", principal_id="r", scopes=SCOPES)  # no review scope
    accepted = service.review(NS, target["candidate_id"], "accept", "GLEIF registeredAs matches the CH number",
                              principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["decision_id"].startswith("entity-decision:")
    history = EntityHistoryStore(env.conn).history(NS, accepted["left_entity"], scopes=HISTORY)["items"]
    assert history[0]["decision_type"] == "match" and history[0]["payload"]["policy"]["merge"] is False
    assert env.conn.execute("SELECT count(*) FROM ownership_record_revisions").fetchone()[0] == before
    assert env.conn.execute("SELECT count(*) FROM entity_history_redirects").fetchone()[0] == 0  # no merge redirect
    assert service.clusters(NS)[UK_KEYS["ch"]] == service.clusters(NS)[UK_KEYS["gleif"]]
    with pytest.raises(OwnershipError):
        service.review(NS, target["candidate_id"], "reject", "twice", principal_id=harness.REVIEWER,
                       scopes=harness.REVIEW_SCOPES)


def test_rejected_and_reverted_matches_leave_both_records_intact_and_auditable(proposed):
    env, service, _ = proposed
    store = OwnershipStore(env.conn)
    decoy = candidate(service, HOLD_KEYS["ch"], harness.DECOY)
    assert decoy["basis"] == "name-jurisdiction"
    rejected = service.review(NS, decoy["candidate_id"], "reject", "different company; the decoy has no CH number",
                              principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)
    assert rejected["state"] == "rejected"
    assert EntityHistoryStore(env.conn).history(NS, rejected["right_entity"], scopes=HISTORY)["items"][0][
        "decision_type"] == "non-match"
    match = candidate(service, UK_KEYS["gleif"], UK_KEYS["bods"])
    service.review(NS, match["candidate_id"], "accept", "same LEI", principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)
    reverted = service.revert(NS, match["candidate_id"], "reviewer error", principal_id=harness.REVIEWER,
                              scopes=harness.REVIEW_SCOPES)
    assert reverted["state"] == "reverted"
    assert [h["state"] for h in reverted["history"]] == ["proposed", "accepted", "reverted"]
    decisions = EntityHistoryStore(env.conn).history(NS, reverted["left_entity"], scopes=HISTORY)["items"]
    assert [d["decision_type"] for d in decisions] == ["match", "undo"]
    assert UK_KEYS["bods"] not in service.clusters(NS)
    for key in (UK_KEYS["gleif"], UK_KEYS["bods"], HOLD_KEYS["ch"], harness.DECOY):
        assert store.by_key(NS, key, principal_id="p", scopes=SCOPES) is not None
    service.revert(NS, rejected["candidate_id"], "reopen", principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)
    with pytest.raises(OwnershipError):
        service.revert(NS, rejected["candidate_id"], "again", principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)


def test_successors_are_events_never_identity_candidates(proposed):
    _, service, result = proposed
    keys = {k for c in result["candidates"] for k in (c["left_key"], c["right_key"])}
    assert f"gleif:lei:{harness.TRADE}" not in keys


def test_opencorporates_links_are_aggregator_evidence(proposed):
    env, service, _ = proposed
    from src.kb.lei import LeiStore

    lei_scopes = {"knowledge:companies:write", f"namespace:{NS}:write"}
    LeiStore(env.conn).link_identity(NS, harness.UK, "opencorporates", "opencorporates:gb/09990002",
                                     "fixture enrichment record", scopes=lei_scopes, principal_id="p")
    result = service.propose(NS, principal_id=harness.PRINCIPAL, scopes=SCOPES, lei_namespace=NS)
    link = next(c for c in result["candidates"] if c["right_key"].startswith("opencorporates:") or
                c["left_key"].startswith("opencorporates:"))
    assert link["basis"] == "cross-referenced-identifier"
    assert "not an authoritative substitute" in link["evidence"][0]["note"]
