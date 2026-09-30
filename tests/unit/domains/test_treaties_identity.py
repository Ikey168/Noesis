"""Participants and treaties matched across sources through reviewable identity (#2610, TR06)."""

from __future__ import annotations

import pytest

from src.kb.ownership_store import OwnershipError
from src.kb.treaties_identity import TreatiesIdentity, place_key
from tests.unit import treaties_harness as h


@pytest.fixture
def world():
    conn = h.connection()
    clock = h.Clock()
    h.load_all(conn)
    places = h.seed_places(conn)
    identity = TreatiesIdentity(conn, now=clock)
    result = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace=h.NS)
    yield conn, identity, places, result
    conn.close()


def by_pair(result, left, right):
    return next(c for c in result["candidates"] if set(c["records"]) == {left, right})


def test_published_codes_come_before_names_and_nothing_is_accepted(world):
    _, identity, places, result = world
    assert {c["state"] for c in result["candidates"]} == {"proposed"}
    code = by_pair(result, "treaties:eu-cellar:participant:xea", place_key(places["XEA"]))
    assert code["basis"] == "exact-identifier" and code["method"] == "iso3166-code"
    assert code["evidence"][0]["scheme"] == "iso3166-1-alpha3" and code["evidence"][0]["value"] == "XEA"
    named = by_pair(result, "treaties:untc:participant:exampland", place_key(places["XEA"]))
    assert named["basis"] == "name-jurisdiction" and named["method"] == "name-as-published"
    assert named["confidence"] < code["confidence"]
    # a participant with a published code is never paired by name
    assert not [c for c in result["candidates"] if "treaties:eu-cellar:participant:xea" in c["records"]
                and c["method"] == "name-as-published"]
    cross = by_pair(result, "treaties:untc:participant:southland", "treaties:coe:participant:southland")
    assert cross["method"] == "name-as-published"
    eu = by_pair(result, "treaties:untc:participant:european-union", "treaties:eu-cellar:participant:eu")
    assert eu["method"] == "eu-designation"
    assert not [c for c in result["candidates"] if "treaties:eu-cellar:participant:eu" in c["records"]
                and any(r.startswith("geospatial:") for r in c["records"])]
    treaty = by_pair(result, h.EU, h.COE)
    assert treaty["basis"] == "cross-referenced-identifier" and treaty["method"] == "published-cross-reference"
    assert treaty["evidence"][0]["scheme"] == "cets" and treaty["evidence"][0]["value"] == "999"
    assert not [c for c in result["candidates"] if h.UNTC in c["records"]]  # no cross-reference, no candidate
    again = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, geo_namespace=h.NS)
    assert again["proposed"] == []  # idempotent


def test_review_accept_reject_and_revert_are_entity_identity_decisions(world):
    conn, identity, places, result = world
    named = by_pair(result, "treaties:untc:participant:exampland", place_key(places["XEA"]))
    with pytest.raises(OwnershipError):
        identity.review(h.NS, named["candidate_id"], "accept", "names agree", principal_id="reviewer",
                        scopes=h.SCOPES)  # no review scope
    accepted = identity.review(h.NS, named["candidate_id"], "accept", "UNTC lists Exampland; the place is Exampland",
                               principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["decision_id"] and accepted["reviewer"] == "reviewer"
    decisions = conn.execute("SELECT count(*) FROM entity_identity_decisions").fetchone()[0]
    assert decisions >= 1
    code = by_pair(result, "treaties:eu-cellar:participant:xea", place_key(places["XEA"]))
    identity.review(h.NS, code["candidate_id"], "accept", "published ISO code", principal_id="reviewer",
                    scopes=h.REVIEW_SCOPES)
    reached = {e["record_key"] for e in identity.equivalents(h.NS, "treaties:untc:participant:exampland",
                                                             scopes=h.SCOPES)}
    assert reached == {"treaties:eu-cellar:participant:xea"}  # through the shared, accepted place
    reverted = identity.revert(h.NS, named["candidate_id"], "reviewer withdrew the decision",
                               principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and [s["state"] for s in reverted["history"]][-2:] == ["accepted",
                                                                                                  "reverted"]
    assert identity.equivalents(h.NS, "treaties:untc:participant:exampland", scopes=h.SCOPES) == []
    cross = by_pair(result, "treaties:untc:participant:southland", "treaties:coe:participant:southland")
    rejected = identity.review(h.NS, cross["candidate_id"], "reject", "not verified", principal_id="reviewer",
                               scopes=h.REVIEW_SCOPES)
    assert rejected["state"] == "rejected"


def test_unmatched_records_stay_visible_and_absent_places_are_reported(world):
    _, identity, _, _ = world
    unmatched = identity.unmatched(h.NS, scopes=h.SCOPES)
    assert "treaties:untc:participant:oldland" in {p["record_key"] for p in unmatched["participants"]}
    assert h.UNTC in {t["record_key"] for t in unmatched["treaties"]}
    fresh = h.connection()
    h.load_all(fresh)
    report = TreatiesIdentity(fresh).propose(h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert {"provider": "geospatial.core", "reason": "no geospatial namespace given"} in report["unavailable"]
    fresh.close()
