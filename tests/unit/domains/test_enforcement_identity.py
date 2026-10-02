"""Respondents matched through reviewable identity; authorities as source identities (#2651, EN07)."""

from __future__ import annotations

import pytest

from src.kb.enforcement import EnforcementError
from src.kb.enforcement_identity import EnforcementIdentity
from src.kb.ownership_identity import FOREIGN_KEY_PREFIXES, OwnershipIdentityService
from tests.unit import enforcement_harness as h


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.ownership(conn)
    h.load_all(conn)
    identity = EnforcementIdentity(conn)
    proposed = identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    yield conn, identity, proposed
    conn.close()


def test_published_identifiers_come_first_and_nothing_is_accepted_automatically(loaded):
    conn, identity, proposed = loaded
    candidates = proposed["candidates"]
    assert candidates and all(c["state"] == "proposed" for c in candidates)
    sec = [c for c in candidates if c["subject_key"].startswith("enforcement:respondent:us-sec:LR-99901")]
    exact = [c for c in sec if c["method"] == "exact-identifier"]
    assert [c["ownership_key"] for c in exact] == [h.SEC_FILER]
    assert exact[0]["evidence"][0]["identifiers"][0]["scheme"] == "sec-cik"
    assert exact[0]["low_evidence"] is False and exact[0]["confidence"] > max(
        c["confidence"] for c in sec if c["method"] == "name-jurisdiction")
    names = [c for c in candidates if c["method"] == "name-jurisdiction"]
    assert names and all(c["low_evidence"] for c in names)
    # Proposing again is idempotent.
    again = identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    assert again["proposed"] == [] and len(again["candidates"]) == len(candidates)
    # Nothing is merged: the ownership clusters are those of the ownership decisions alone.
    assert "enforcement:" in FOREIGN_KEY_PREFIXES
    assert not any(k.startswith("enforcement:") for k in OwnershipIdentityService(conn).clusters(h.OWN_NS))


def test_authorities_are_source_identities_and_individuals_are_never_subjects(loaded):
    _, identity, proposed = loaded
    authorities = {a["authority"] for a in proposed["authorities"]}
    assert authorities == {"us-sec", "uk-fca", "us-epa", "eu-dpa-nl", "eu-dpa-ie"}
    assert all(a["identity"].startswith("source identity") for a in proposed["authorities"])
    names = {s["name_as_published"] for s in identity.subjects(h.NS, scopes=h.SCOPES)}
    assert "Jordan Placeholder" not in names and "Casey Placeholder" not in names
    assert "Northwind Securities LLC" in names


def test_review_accept_reject_and_revert_with_reviewer_and_unmatched_stay_visible(loaded):
    _, identity, proposed = loaded
    exact = next(c for c in proposed["candidates"] if c["method"] == "exact-identifier")
    with pytest.raises(EnforcementError):  # review needs the review scope
        identity.review(h.NS, exact["candidate_id"], "accept", "CIK agrees", principal_id="analyst",
                        scopes=h.SCOPES)
    accepted = identity.review(h.NS, exact["candidate_id"], "accept", "CIK agrees", principal_id="reviewer",
                               scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "reviewer" and accepted["decision_id"]
    assert identity.accepted_links(h.NS, [h.SEC_FILER], scopes=h.SCOPES)[0]["candidate_id"] == exact["candidate_id"]
    reverted = identity.revert(h.NS, exact["candidate_id"], "filer record superseded", principal_id="reviewer",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and [e["state"] for e in reverted["history"]][-1] == "reverted"
    assert identity.accepted_links(h.NS, [h.SEC_FILER], scopes=h.SCOPES) == []
    decoy = next(c for c in proposed["candidates"] if "decoy" in c["ownership_key"])
    assert identity.review(h.NS, decoy["candidate_id"], "reject", "different register entity",
                           principal_id="reviewer", scopes=h.REVIEW_SCOPES)["state"] == "rejected"
    unmatched = {u["name_as_published"]: u for u in identity.unmatched(h.NS, scopes=h.SCOPES)}
    assert {"Northwind Securities LLC", "Northwind Brokers Limited", "Northwind Chemicals Inc."} <= set(unmatched)
    assert unmatched["Northwind Securities LLC"]["pending_candidates"] == 0
    assert "Exampla Holdings plc" in unmatched  # nothing accepted for it any more
    with pytest.raises(EnforcementError):
        identity.review(h.NS, "not-a-candidate", "accept", "x", principal_id="reviewer", scopes=h.REVIEW_SCOPES)


def test_candidates_across_ownership_clusters_are_reported_as_conflicts(loaded):
    _, identity, _ = loaded
    conflicts = identity.conflicts(h.NS, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
    holdings = [c for c in conflicts if c["subject_key"].startswith("enforcement:respondent:us-sec:LR-99901")]
    assert holdings and len(holdings[0]["clusters"]) > 1 and holdings[0]["status"] == "conflict"
