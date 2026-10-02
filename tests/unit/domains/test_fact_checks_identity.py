"""Fact-checks reviewable identity: claimants, claims and publishers are proposed, never auto-merged (#2688)."""

from __future__ import annotations

import pytest

from src.kb.fact_checks_identity import FactCheckIdentity
from src.kb.fact_checks_records import FactCheckError
from tests.unit import fact_checks_harness as h


def proposed(conn):
    return FactCheckIdentity(conn).propose(h.NS, principal_id="alice", scopes=h.SCOPES)


def by(candidates, **match):
    return [c for c in candidates if all(c[k] == v for k, v in match.items())]


def test_candidates_carry_method_evidence_and_confidence_and_nothing_is_accepted():
    result = proposed(h.world())
    candidates = result["candidates"]
    assert candidates and {c["state"] for c in candidates} == {"proposed"}
    assert result["unavailable"] == []
    for candidate in candidates:
        assert candidate["method"] and candidate["confidence"] and candidate["evidence"][0]["method"]
        assert candidate["review_state"] == "unreviewed-candidate"
    (identifier,) = by(candidates, match_kind="claimant", right_key="ent-wikidata-q99999901",
                       method="published-identifier")
    assert identifier["left_key"] == "fact-check:claimant:wikidata:Q99999901" and identifier["confidence"] == 0.95
    (named,) = by(candidates, left_key="fact-check:claimant:name:robin-sample")
    assert named["method"] == "name-as-published" and named["confidence"] < identifier["confidence"]
    (publisher,) = by(candidates, match_kind="publisher")
    assert publisher["method"] == "published-domain" and publisher["evidence"][0]["domain"] == "factdesk.example"
    claims = by(candidates, match_kind="claim")
    assert {(c["right_key"], c["method"]) for c in claims} == {
        ("claim-widgets", "shared-appearance-url"), ("claim-weather", "shared-appearance-url"),
        ("claim-widgets", "quoted-text-overlap")}
    assert all("without review" in c["evidence"][0]["note"] for c in claims)


def test_published_identifiers_are_used_before_names():
    candidates = proposed(h.world())["candidates"]
    wikidata_subject = by(candidates, left_key="fact-check:claimant:wikidata:Q99999901")
    assert [c["method"] for c in wikidata_subject] == ["published-identifier"]  # no name candidate beside it


def test_review_accept_reject_and_revert_are_entity_history_decisions():
    conn = h.world()
    identity = FactCheckIdentity(conn)
    candidates = proposed(conn)["candidates"]
    (widgets,) = by(candidates, right_key="claim-widgets", method="shared-appearance-url")
    (weather,) = by(candidates, right_key="claim-weather")
    accepted = identity.review(h.NS, widgets["candidate_id"], "accept", "same claim, same article",
                               principal_id="rev", scopes=h.REVIEW_SCOPES)
    rejected = identity.review(h.NS, weather["candidate_id"], "reject", "different claim in the same article",
                               principal_id="rev", scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "rev" and accepted["decision_id"]
    assert rejected["review_state"] == "reviewed-non-match"
    decisions = dict(conn.execute("SELECT decision_id, decision_type FROM entity_identity_decisions").fetchall())
    assert decisions[accepted["decision_id"]] == "match" and decisions[rejected["decision_id"]] == "non-match"
    reverted = identity.revert(h.NS, widgets["candidate_id"], "reviewer error", principal_id="rev",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and [s["state"] for s in reverted["history"]] == [
        "proposed", "accepted", "reverted"]
    assert identity.accepted(h.NS, "claim", scopes=h.SCOPES) == []
    # re-proposing never changes a reviewed candidate without new evidence, and nothing is accepted automatically
    again = proposed(conn)
    assert {c["candidate_id"]: c["state"] for c in again["candidates"]}[weather["candidate_id"]] == "rejected"
    with pytest.raises(FactCheckError):
        identity.review(h.NS, widgets["candidate_id"], "accept", "x", principal_id="rev", scopes=h.SCOPES)


def test_unmatched_records_stay_visible_and_absent_owners_are_reported():
    conn = h.world(news=False, sources=False)
    result = proposed(conn)
    assert {u["target"] for u in result["unavailable"]} == {"canonical_entities", "argument_claims",
                                                           "source_identities"}
    unmatched = result["unmatched"]
    assert len(unmatched["claim"]) == 5 and {u["state"] for u in unmatched["claim"]} == {"unmatched"}
    assert "fact-check:publisher:fabrikam-facts.example" in {u["subject_key"] for u in unmatched["publisher"]}
    conn = h.accepted_world(v2=False)
    left = FactCheckIdentity(conn).unmatched(h.NS, scopes=h.SCOPES)
    assert "fact-check:claimant:name:robin-sample" in {u["subject_key"] for u in left["claimant"]}
    assert "fact-check:publisher:factdesk.example" not in {u["subject_key"] for u in left["publisher"]}


def test_social_accounts_are_never_subjects():
    subjects = FactCheckIdentity(h.world()).subjects(h.NS, scopes=h.SCOPES)
    text = repr(subjects)
    assert "robinsample_fake" not in text and "x.com" not in text
