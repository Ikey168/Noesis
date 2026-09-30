"""Organisations and participants matched through reviewable identity; researchers never matched (#2614)."""

from __future__ import annotations

import pytest

from src.kb.ownership_identity import FOREIGN_KEY_PREFIXES, OwnershipIdentityService
from src.kb.research_entities_identity import ResearchEntitiesIdentity
from src.kb.research_entities_records import ResearchEntitiesError
from tests.unit import research_entities_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn)
    own = h.seed_ownership(conn)
    identity = ResearchEntitiesIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    return conn, identity, proposed, own


def by(proposed, left, right_contains):
    return next(m for m in proposed["matches"] if m["left"]["key"].endswith(left)
                and right_contains in m["right"]["key"])


def test_proposals_carry_method_evidence_and_confidence_and_nothing_is_accepted(env):
    _, _, proposed, _ = env
    assert proposed["matches"] and {m["state"] for m in proposed["matches"]} == {"proposed"}
    uni = by(proposed, "0re1ab101", "pic:999999901")
    assert (uni["method"], uni["confidence"]) == ("website-domain-country", 0.6)
    assert uni["evidence"]["hosts"] == ["uni-beispielstadt.example"]
    # Published identifiers before names: the ISNI decides the ownership match, the VAT number the participant's.
    assert by(proposed, "0re1ab101", "rent-uni")["method"] == "exact-identifier"
    assert by(proposed, "pic:999999903", "rent-exampla")["method"] == "exact-identifier"
    lei = by(proposed, "0re1ab303", "gleif:")
    assert lei["method"] == "name-jurisdiction" and lei["low_evidence"] is True
    assert all(m["evidence"] for m in proposed["matches"])
    assert "research-entities:" in FOREIGN_KEY_PREFIXES


def test_unmatched_records_stay_visible(env):
    _, identity, proposed, _ = env
    unmatched = proposed["unmatched"]
    assert h.A2 in {o["ror_id"] for o in unmatched["organisations"]}
    assert {p["pic"] for p in unmatched["participants"]} == {"999999901", "999999902", "999999903"}
    uni = by(proposed, "0re1ab101", "pic:999999901")
    identity.review(h.NS, uni["match_id"], "accept", "same website and country", principal_id="rev",
                    scopes=h.SCOPES)
    after = identity.unmatched(h.NS, scopes=h.SCOPES)
    assert "999999901" not in {p["pic"] for p in after["participants"]}
    assert h.A1 not in {o["ror_id"] for o in after["organisations"]}


def test_review_and_revert_are_entity_history_decisions_and_never_merge(env):
    conn, identity, proposed, _ = env
    uni = by(proposed, "0re1ab101", "pic:999999901")
    accepted = identity.review(h.NS, uni["match_id"], "accept", "same website", principal_id="rev", scopes=h.SCOPES)
    assert accepted["state"] == "accepted" and accepted["decision_id"]
    assert identity.accepted_pics(h.NS, h.A1) == [{"pic": "999999901", "match_id": uni["match_id"],
                                                  "method": "website-domain-country",
                                                  "decision_id": accepted["decision_id"]}]
    decision = conn.execute("SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?",
                            [accepted["decision_id"]]).fetchone()
    assert decision == ("match",)
    reverted = identity.revert(h.NS, uni["match_id"], "wrong campus", principal_id="rev", scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and identity.accepted_pics(h.NS, h.A1) == []
    with pytest.raises(ResearchEntitiesError):
        identity.revert(h.NS, uni["match_id"], "again", principal_id="rev", scopes=h.SCOPES)
    ownership = by(proposed, "0re1ab101", "rent-uni")
    done = identity.review(h.NS, ownership["match_id"], "accept", "ISNI agrees", principal_id="rev", scopes=h.SCOPES)
    assert done["kind"] == "ownership" and done["state"] == "accepted"
    # The ownership namespace's own clusters are never regrouped by this feature's links.
    assert OwnershipIdentityService(conn).clusters(h.NS) == {}


def test_reproposing_is_idempotent_and_scopes_are_enforced(env):
    _, identity, proposed, _ = env
    again = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert again["proposed"] == [] and len(again["matches"]) == len(proposed["matches"])
    with pytest.raises(ResearchEntitiesError):
        identity.propose(h.NS, principal_id="x", scopes=h.READ_ONLY)
    with pytest.raises(ResearchEntitiesError):
        identity.review(h.NS, proposed["matches"][0]["match_id"], "accept", "", principal_id="x", scopes=h.SCOPES)


def test_researchers_are_never_matched_or_merged(env):
    _, identity, proposed, _ = env
    assert not [m for m in proposed["matches"] if "orcid" in m["left"]["key"] or "orcid" in m["right"]["key"]]
    with pytest.raises(ResearchEntitiesError) as refused:
        identity.refuse_researcher_match()
    assert refused.value.code == "researcher_identity_excluded"
