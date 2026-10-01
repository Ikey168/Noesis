"""Claimants, claims and publishers matched through reviewable assertions (#2688, FC06)."""

from __future__ import annotations

import pytest

from src.kb.fact_checks_identity import FactCheckIdentity
from src.kb.fact_checks_records import FactCheckError
from tests.unit import fact_checks_harness as h


def world():
    conn = h.connection()
    h.load_all(conn)
    h.load_news(conn)
    source_id = h.load_source_identity(conn)
    identity = FactCheckIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.REVIEW_SCOPES)
    return conn, identity, proposed, source_id


def one(matches, **wanted):
    found = [m for m in matches if all(m[k] == v for k, v in wanted.items())]
    assert len(found) == 1, (wanted, [(m["match_kind"], m["right_key"], m["method"]) for m in matches])
    return found[0]


def test_matches_carry_method_evidence_and_confidence_and_published_identifiers_come_first():
    _, _, proposed, source_id = world()
    matches = proposed["matches"]
    assert all(m["state"] == "proposed" and m["review_state"] == "unreviewed-candidate" for m in matches)
    mayor = one(matches, match_kind="claimant-entity", left_key="fact-checks:claimant:mayor-alex-example")
    assert mayor["method"] == "published-identifier" and mayor["right_key"] == "ent-alex-example"
    assert mayor["evidence"][0]["identifier_as_published"] == "https://www.wikidata.org/wiki/Q999999901"
    assert mayor["confidence"] > one(matches, left_key="fact-checks:claimant:example-energy-council")["confidence"]
    seals = [m for m in matches if m["match_kind"] == "claim-argument" and m["right_key"] == "argument-claim:claim-seals"]
    assert {m["method"] for m in seals} == {"appearance-url"} and len(seals) == 2
    assert one(matches, match_kind="claim-argument", right_key="argument-claim:claim-array")["method"] == \
        "lexical-overlap"
    publisher = one(matches, match_kind="publisher-source")
    assert publisher["right_key"] == source_id and publisher["method"] == "published-domain"
    assert all(m["evidence"][0]["left"]["revision_id"] for m in matches)
    # generic claimants are never proposed
    assert not [m for m in matches if "social-media" in m["left_key"]]
    assert proposed["unavailable"] == []


def test_nothing_is_merged_and_proposing_again_is_idempotent():
    conn, identity, proposed, _ = world()
    again = identity.propose(h.NS, principal_id="alice", scopes=h.REVIEW_SCOPES)
    assert again["proposed"] == []
    assert len(again["matches"]) == len(proposed["matches"])
    assert conn.execute("SELECT count(*) FROM canonical_entities").fetchone()[0] == 2  # no entity created or merged


def test_review_accept_reject_and_revert_with_entity_history_decisions():
    conn, identity, proposed, _ = world()
    mayor = one(proposed["matches"], match_kind="claimant-entity", right_key="ent-alex-example")
    accepted = identity.review(h.NS, mayor["match_id"], "accept", "Wikidata id agrees", principal_id="rev",
                               scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "rev" and accepted["decision_id"]
    decision = conn.execute("SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?",
                            [accepted["decision_id"]]).fetchone()
    assert decision == ("match",)
    reverted = identity.revert(h.NS, mayor["match_id"], "reviewer error", principal_id="rev", scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and [s["state"] for s in reverted["history"]] == [
        "proposed", "accepted", "reverted"]
    lexical = one(proposed["matches"], method="lexical-overlap")
    rejected = identity.review(h.NS, lexical["match_id"], "reject", "different claim", principal_id="rev",
                               scopes=h.REVIEW_SCOPES)
    assert rejected["state"] == "rejected" and rejected["decision_id"] is None  # a claim match is not an identity
    with pytest.raises(FactCheckError) as twice:
        identity.review(h.NS, lexical["match_id"], "accept", "again", principal_id="rev", scopes=h.REVIEW_SCOPES)
    assert twice.value.code == "invalid_state"


def test_claimant_matches_need_the_claimant_scope():
    _, identity, proposed, _ = world()
    mayor = one(proposed["matches"], match_kind="claimant-entity", right_key="ent-alex-example")
    scopes = h.SCOPES | {h.REVIEW}
    with pytest.raises(FactCheckError) as refused:
        identity.review(h.NS, mayor["match_id"], "accept", "ok", principal_id="rev", scopes=scopes)
    assert refused.value.code == "unauthorized"
    assert not [m for m in identity.matches(h.NS, scopes=h.SCOPES) if m["match_kind"] == "claimant-entity"]
    assert "withheld" in identity.unmatched(h.NS, scopes=h.SCOPES)["claimants"]
    fresh = h.connection()
    h.load_all(fresh)
    h.load_news(fresh)
    without = FactCheckIdentity(fresh).propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert not [m for m in without["matches"] if m["match_kind"] == "claimant-entity"]
    assert any(u["target"] == "canonical_entities" for u in without["unavailable"])


def test_unmatched_subjects_stay_visible_and_absent_targets_are_reported():
    _, identity, proposed, _ = world()
    for match in proposed["matches"]:
        if match["method"] != "lexical-overlap":
            identity.review(h.NS, match["match_id"], "accept", "ok", principal_id="rev", scopes=h.REVIEW_SCOPES)
    unmatched = identity.unmatched(h.NS, scopes=h.REVIEW_SCOPES)
    texts = {c["claim_text_as_quoted"] for c in unmatched["claims"]}
    assert "A photo shows a tidal turbine washed up on Example Beach." in texts
    assert h.SEALS not in texts
    assert {"fact-checks:claimant:social-media-users", "fact-checks:claimant:example-grid-operator"} <= {
        c["key"] for c in unmatched["claimants"]}
    assert "fact-checks:publisher-site:verifica.example.net" in {p["key"] for p in unmatched["publishers"]}
    bare = h.connection()
    h.load_all(bare)
    report = FactCheckIdentity(bare).propose(h.NS, principal_id="alice", scopes=h.REVIEW_SCOPES)
    assert report["matches"] == []
    assert {u["target"] for u in report["unavailable"]} == {"canonical_entities", "argument_claims",
                                                            "source_identities"}
