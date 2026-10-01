"""Advertisers and funding entities matched through reviewable identity (#2616)."""

from __future__ import annotations

import pytest

from src.kb.ownership_identity import FOREIGN_KEY_PREFIXES, OwnershipIdentityService
from src.kb.ownership_store import OwnershipError
from src.kb.platform_transparency_identity import (
    PlatformTransparencyIdentity,
    funding_key,
)
from src.kb.platform_transparency_records import PlatformTransparencyError
from tests.unit import platform_transparency_harness as h

GOOGLE_CAMPAIGN = "platform-transparency:google:advertiser:AR10000000000000000001"
PERSON_PAGE = "platform-transparency:meta:advertiser:999000003"


@pytest.fixture()
def proposed():
    conn = h.connection()
    h.load_all(conn)
    h.load_other_packs(conn)
    identity = PlatformTransparencyIdentity(conn)
    result = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    return conn, identity, result


def by_records(candidates):
    return {tuple(sorted(c["records"])): c for c in candidates}


def test_published_identifiers_come_before_names_and_nothing_is_accepted_automatically(proposed):
    _, identity, result = proposed
    found = by_records(result["candidates"])
    exact = found[("campaign-finance:fec:committee:C00999901", GOOGLE_CAMPAIGN)]
    assert exact["basis"] == "exact-identifier" and exact["method"] == "published-id"
    assert exact["evidence"][0]["value"] == "C00999901" and exact["evidence"][0]["published_by"]
    assert exact["confidence"] > found[("gb-coh:09990002", "platform-transparency:meta:advertiser:999000001")][
        "confidence"]
    names = [c for c in result["candidates"] if c["method"] == "name+country"]
    assert names and all(c["basis"] == "name-jurisdiction" for c in names)
    assert {c["state"] for c in result["candidates"]} == {"proposed"}
    assert funding_key("Paid for by Example Holdings Ltd") in {r for c in names for r in c["records"]}
    again = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert again["proposed"] == []  # idempotent
    assert "platform-transparency:" in FOREIGN_KEY_PREFIXES  # never regroups ownership entities


def test_review_accept_and_revert_are_recorded_decisions_and_unmatched_subjects_stay_visible(proposed):
    conn, identity, result = proposed
    exact = by_records(result["candidates"])[("campaign-finance:fec:committee:C00999901", GOOGLE_CAMPAIGN)]
    with pytest.raises(OwnershipError):  # no review scope
        identity.review(h.NS, exact["candidate_id"], "accept", "ok", principal_id="bob", scopes=h.SCOPES)
    accepted = identity.review(h.NS, exact["candidate_id"], "accept", "FEC id published by Google",
                               principal_id="rev", scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "rev" and accepted["decision_id"]
    assert identity.identity(h.NS, GOOGLE_CAMPAIGN, scopes=h.SCOPES)["state"] == "matched"
    assert identity.subjects_for(h.NS, "campaign-finance:fec:committee:C00999901", scopes=h.SCOPES)[0][
        "record_key"] == GOOGLE_CAMPAIGN
    reverted = identity.revert(h.NS, exact["candidate_id"], "second look", principal_id="rev",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and [s["state"] for s in reverted["history"]][-1] == "reverted"
    assert identity.identity(h.NS, GOOGLE_CAMPAIGN, scopes=h.SCOPES)["state"] == "unmatched"
    unmatched = {u["record_key"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)["unmatched"]}
    assert {GOOGLE_CAMPAIGN, PERSON_PAGE} <= unmatched
    with pytest.raises(PlatformTransparencyError):
        identity.review(h.NS, "own-idc:not-ours", "accept", "x", principal_id="rev", scopes=h.REVIEW_SCOPES)
    # the shared state machine holds the decision; records are never merged
    service = OwnershipIdentityService(conn, initialize=False)
    assert service.candidates(h.NS, scopes=h.SCOPES, record_key=GOOGLE_CAMPAIGN)[0]["state"] == "reverted"


def test_natural_persons_are_never_targets_and_absent_providers_are_reported(proposed):
    _, _, result = proposed
    records = {r for c in result["candidates"] for r in c["records"]}
    assert not any(r.startswith(("campaign-finance:fec:candidate:", "elections:")) for r in records)
    assert PERSON_PAGE not in records  # a person-named page matches nothing
    alone = h.connection()
    h.load_all(alone)
    empty = PlatformTransparencyIdentity(alone).propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert empty["candidates"] == []
    assert {u["provider"] for u in empty["unavailable"]} == {"political.campaign-finance", "political.lobbying",
                                                             "ownership.core"}
    assert "never targets" in empty["individuals"]
