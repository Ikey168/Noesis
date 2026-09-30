"""Platform-transparency identity: advertisers and funding entities matched through reviewable decisions (#2616)."""

from __future__ import annotations

import pytest

from src.kb.ownership_identity import FOREIGN_KEY_PREFIXES
from src.kb.platform_transparency_identity import PlatformTransparencyIdentity
from src.kb.platform_transparency_records import (
    PlatformTransparencyError,
    PlatformTransparencyStore,
)
from tests.unit import platform_transparency_harness as h

FUND = f"platform-transparency:google:advertiser:{h.FUND}"
PARTY = f"platform-transparency:meta:advertiser:{h.PARTY_PAGE}"


def proposed(conn, **kwargs):
    return PlatformTransparencyIdentity(conn).propose(
        h.NS, principal_id="alice", scopes=h.SCOPES, **{"ownership_namespace": h.OWN_NS,
                                                        "campaign_finance_namespace": h.CF_NS,
                                                        "lobbying_namespace": h.CF_NS,
                                                        "elections_namespace": h.CF_NS, **kwargs})


def pairs(result):
    return {(c["method"], tuple(sorted(c["records"]))) for c in result["candidates"]}


def test_published_identifiers_come_first_and_nothing_is_accepted_automatically():
    conn = h.world()
    result = proposed(conn)
    assert all(c["state"] == "proposed" and c["review_state"] == "unreviewed-candidate" for c in result["candidates"])
    assert ("published-id", ("campaign-finance:fec:committee:C00999903", FUND)) in pairs(result)
    fund = next(c for c in result["candidates"] if FUND in c["records"])
    assert fund["basis"] == "exact-identifier" and fund["confidence"] == 0.95
    assert fund["evidence"][0]["scheme"] == "fec-committee-id" and fund["evidence"][0]["left"]["cited"]
    # a published id suppresses a weaker name match for the same advertiser
    assert not [c for c in result["candidates"] if FUND in c["records"] and c["method"] != "published-id"]
    names = {p for p in pairs(result) if p[0] == "name+jurisdiction"}
    assert ("name+jurisdiction", ("elections:gb-general:2099-05-07:party:example-party", PARTY)) in names
    assert ("name+jurisdiction", ("gb-coh:09990002",
                                  f"platform-transparency:meta:advertiser:{h.HOLDINGS_PAGE}")) in names
    assert ("name+jurisdiction", ("lobbying:uk-orcl:ORCL0099",
                                  "platform-transparency:meta:funder:example-public-affairs-ltd:gb")) in names
    assert all(c["confidence"] < 0.5 for c in result["candidates"] if c["method"] == "name+jurisdiction")
    assert result["unavailable"] == []
    again = proposed(conn)
    assert again["proposed"] == [] and pairs(again) == pairs(result)  # idempotent


def test_natural_persons_are_never_targets_even_with_an_equal_name():
    conn = h.world()
    fetched = h.adapter(h.META).fetch_page({"operation": "selection", "parameters": {}}, cursor=None)
    ad = next(r["platform_transparency_record"] for r in fetched.records
              if r["platform_transparency_record"]["record_kind"] == "ad")
    person = {**ad, "record_key": "platform-transparency:meta:ad:990000000000999",
              "advertiser_key": "platform-transparency:meta:advertiser:100000000000009",
              "fields": {**ad["fields"], "ad_id": "990000000000999", "page_id": "100000000000009",
                         "advertiser_as_declared": "Alex Sample", "funding_entity_as_declared": "Alex Sample"}}
    PlatformTransparencyStore(conn).project(h.NS, [person], run_id="person", source_id=h.META)
    result = proposed(conn)
    candidates = [c for c in result["candidates"] if any("100000000000009" in r or "alex-sample" in r
                                                          for r in c["records"])]
    assert candidates == []  # the elections candidate record "Alex Sample" is a natural person, never a target
    assert "never matched" in result["individuals"]


def test_absent_providers_are_reported_and_unmatched_subjects_stay_visible():
    conn = h.world(elections=False, lobbying=False, ownership=False, campaign_finance=False)
    result = proposed(conn, ownership_namespace=None)
    assert result["candidates"] == []
    assert {u["provider"] for u in result["unavailable"]} == {
        "political.campaign-finance", "political.elections", "political.lobbying", "ownership.core"}
    unmatched = PlatformTransparencyIdentity(conn).unmatched(h.NS, scopes=h.SCOPES)["unmatched"]
    assert {u["record_key"] for u in unmatched} >= {FUND, PARTY, f"platform-transparency:google:advertiser:{h.CIVIC}"}
    assert {u["kind"] for u in unmatched} == {"advertiser", "funding-entity"}


def test_review_accepts_rejects_and_reverts_without_touching_records():
    conn = h.world()
    identity = PlatformTransparencyIdentity(conn)
    candidates = {c["method"]: c for c in proposed(conn)["candidates"]}
    fund = candidates["published-id"]
    accepted = identity.review(h.NS, fund["candidate_id"], "accept", "FEC id agrees", principal_id="rev",
                               scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "rev" and accepted["decision_id"]
    other = identity.accepted(h.NS, accepted["records"][1], scopes=h.SCOPES)
    assert [a["method"] for a in other] == ["published-id"]
    reverted = identity.revert(h.NS, fund["candidate_id"], "wrong committee", principal_id="rev",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and [s["state"] for s in reverted["history"]][-1] == "reverted"
    assert identity.identity(h.NS, FUND, scopes=h.SCOPES)["state"] == "unmatched"
    with pytest.raises(Exception):  # noqa: B017 - review needs the review scope
        identity.review(h.NS, candidates["name+jurisdiction"]["candidate_id"], "accept", "x", principal_id="bob",
                        scopes=h.SCOPES)
    with pytest.raises(PlatformTransparencyError):
        identity.review(h.NS, "own-idc:not-ours", "accept", "x", principal_id="rev", scopes=h.REVIEW_SCOPES)
    assert PlatformTransparencyStore(conn).records(h.NS, scopes=h.SCOPES, record_keys=[FUND])


def test_platforms_are_source_identities_registered_idempotently():
    conn = h.world(elections=False, lobbying=False, ownership=False, campaign_finance=False)
    identity = PlatformTransparencyIdentity(conn)
    first = identity.register_platforms(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert {p["platform"]: p["display_name"] for p in first} == {
        "example-market": "Example Market", "example-video": "Example Video", "google": "Google",
        "meta": "Meta (Facebook, Instagram)"}
    again = identity.register_platforms(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert all(p["idempotent"] for p in again) and [p["source_id"] for p in again] == [p["source_id"] for p in first]
    assert not [c for c in proposed(conn)["candidates"] if any(":platform:" in r for r in c["records"])]
    assert "platform-transparency:" in FOREIGN_KEY_PREFIXES
