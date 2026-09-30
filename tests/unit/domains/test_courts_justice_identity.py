"""Courts, organisational parties and statistic places through reviewable identity (#2406)."""

from __future__ import annotations

import pytest

from src.kb.courts_justice import CourtsJusticeError
from src.kb.courts_justice_identity import CourtsIdentity, JusticePlaces
from src.kb.ownership_store import OwnershipError
from tests.unit import courts_justice_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    yield connection
    connection.close()


def test_courts_are_mapped_by_courtlistener_id(conn):
    courts = {c["court_id"]: c for c in CourtsIdentity(conn).courts(h.NS, scopes=h.SCOPES)}
    assert set(courts) == {"dcd", "cadc"}
    assert courts["dcd"]["court_key"] == "courts:court:courtlistener:dcd"
    assert courts["dcd"]["published_identifiers"][0] == {"scheme": "courtlistener-court-id", "value": "dcd"}


def test_organisational_parties_are_proposed_never_auto_accepted_and_persons_never(conn):
    owner = h.seed_ownership(conn)
    identity = CourtsIdentity(conn, now=h.Clock())
    parties = identity.parties(h.NS, scopes=h.SCOPES)
    assert {p["name_as_published"] for p in parties} == {"Example Data Co.", "United States Department of Examples"}
    result = identity.propose(h.NS, principal_id="analyst", scopes=h.REVIEW_SCOPES, ownership_namespace=h.NS)
    (candidate,) = result["candidates"]
    assert candidate["state"] == "proposed" and candidate["method"] == "name-jurisdiction"
    assert owner in candidate["records"] and "courts:party:courtlistener:70001:1" in candidate["records"]
    assert identity.accepted_parties(h.NS, owner, scopes=h.SCOPES) == []
    reviewed = identity.review(h.NS, candidate["candidate_id"], "accept", "same company, checked filing",
                               principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert reviewed["state"] == "accepted" and reviewed["reviewer"] == "reviewer"
    (link,) = identity.accepted_parties(h.NS, owner, scopes=h.SCOPES)
    assert link["docket_key"] == h.DOCKET
    identity.revert(h.NS, candidate["candidate_id"], "wrong entity", principal_id="reviewer",
                    scopes=h.REVIEW_SCOPES)
    assert identity.accepted_parties(h.NS, owner, scopes=h.SCOPES) == []
    with pytest.raises((OwnershipError, CourtsJusticeError)):
        identity.review(h.NS, candidate["candidate_id"], "accept", "again", principal_id="reviewer",
                        scopes=h.READ_ONLY)


def test_places_resolve_by_exact_code_with_ambiguity_left_for_review(conn):
    places = h.seed_places(conn)
    resolver = JusticePlaces(conn, now=h.Clock())
    result = resolver.resolve(h.NS, geo_namespace=h.NS, principal_id="analyst", scopes=h.SCOPES)
    by_code = {(r["scheme"], r["code"]): r for r in result["resolutions"]}
    de = by_code[("eurostat-geo", "DE")]
    assert de["status"] == "resolved" and de["selected_place_id"] == places["DE"]
    assert de["mapping_source"]["method"] == "exact published code"
    ex = by_code[("us-state", "EX")]
    assert ex["status"] == "ambiguous" and ex["selected_place_id"] is None and len(ex["candidates"]) == 2
    assert by_code[("fbi-ori", "EX0000100")]["status"] == "unresolved"
    assert resolver.codes_for_place(h.NS, places["EX-a"], scopes=h.SCOPES) == []
    accepted = resolver.review(h.NS, ex["resolution_id"], "accept", "current boundary", principal_id="reviewer",
                               scopes=h.REVIEW_SCOPES, place_id=places["EX-a"])
    assert accepted["review_state"] == "accepted"
    assert [c["code"] for c in resolver.codes_for_place(h.NS, places["EX-a"], scopes=h.SCOPES)] == ["EX"]
    resolver.review(h.NS, de["resolution_id"], "reject", "not the reporting unit", principal_id="reviewer",
                    scopes=h.REVIEW_SCOPES)
    assert resolver.codes_for_place(h.NS, places["DE"], scopes=h.SCOPES) == []
    again = resolver.resolve(h.NS, geo_namespace=h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert len(again["resolutions"]) == len(result["resolutions"])  # idempotent
