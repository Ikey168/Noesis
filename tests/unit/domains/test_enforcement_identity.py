"""Respondents and authorities through reviewable identity (#2651, EN07)."""

from __future__ import annotations

import pytest

from src.kb.enforcement import EnforcementError
from src.kb.enforcement_identity import EnforcementIdentity, authority_identity
from src.kb.ownership_identity import FOREIGN_KEY_PREFIXES, OwnershipIdentityService
from tests.unit import enforcement_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    h.ownership(conn)
    h.load_all(conn)
    yield conn, EnforcementIdentity(conn)
    conn.close()


def test_identifiers_come_before_names_and_nothing_is_accepted(env):
    _conn, identity = env
    proposed = identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    assert proposed["status"] == "proposed" and proposed["proposed"]
    assert {c["state"] for c in proposed["candidates"]} == {"proposed"}
    sec = [c for c in proposed["candidates"] if c["subject_key"].startswith("enforcement:respondent:us-sec:LR-99901")]
    exact = [c for c in sec if c["method"] == "exact-identifier"]
    assert [c["ownership_key"] for c in exact] == [h.SEC_CIK_ENTITY]
    assert exact[0]["confidence"] > max(c["confidence"] for c in sec if c["method"] != "exact-identifier")
    assert all(c["low_evidence"] for c in sec if c["method"] == "name-jurisdiction")
    assert exact[0]["evidence"][0]["identifiers"][0]["value"] == "0009999101"
    again = identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    assert again["proposed"] == []
    assert "enforcement:" in FOREIGN_KEY_PREFIXES


def test_natural_persons_are_never_subjects_and_unmatched_stay_visible(env):
    _conn, identity = env
    subjects = identity.subjects(h.NS, scopes=h.SCOPES)
    assert subjects and all("natural" not in s["key"] and s["name_as_published"] for s in subjects)
    identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    unmatched = {u["name_as_published"]: u for u in identity.unmatched(h.NS, scopes=h.SCOPES)}
    assert unmatched["Northwind Payments Limited"]["pending_candidates"] == 0
    assert unmatched["Northwind Payments Limited"]["identifiers"][0]["scheme"] == "fca-frn"


def test_review_and_revert_are_recorded_decisions_that_never_regroup_ownership(env):
    conn, identity = env
    proposed = identity.propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="analyst", scopes=h.SCOPES)
    before = OwnershipIdentityService(conn).clusters(h.OWN_NS)
    exact = next(c for c in proposed["candidates"] if c["method"] == "exact-identifier")
    accepted = identity.review(h.NS, exact["candidate_id"], "accept", "CIK stated in the release",
                               principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "reviewer" and accepted["decision_id"]
    assert OwnershipIdentityService(conn).clusters(h.OWN_NS) == before
    links = identity.accepted_links(h.NS, [h.SEC_CIK_ENTITY], scopes=h.SCOPES)
    assert [link["candidate_id"] for link in links] == [exact["candidate_id"]]
    reverted = identity.revert(h.NS, exact["candidate_id"], "wrong filer", principal_id="reviewer",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted"
    assert identity.accepted_links(h.NS, [h.SEC_CIK_ENTITY], scopes=h.SCOPES) == []
    with pytest.raises(EnforcementError):
        identity.review(h.NS, "own-idc:missing", "accept", "x", principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    with pytest.raises(EnforcementError) as caught:
        identity.review(h.NS, exact["candidate_id"], "accept", "x", principal_id="analyst", scopes=h.SCOPES)
    assert caught.value.code == "unauthorized"


def test_authorities_are_source_identities_and_ownership_absence_degrades():
    assert authority_identity("uk-fca")["name"] == "Financial Conduct Authority"
    assert authority_identity("eu-sa-ie")["jurisdiction"] == "IE"
    with pytest.raises(EnforcementError):
        authority_identity("bafin")
    conn = h.connection()
    h.load_all(conn)
    answer = EnforcementIdentity(conn).propose(h.NS, ownership_namespace=h.OWN_NS, principal_id="a", scopes=h.SCOPES)
    assert answer["status"] == "ownership_unavailable" and answer["unmatched"]
