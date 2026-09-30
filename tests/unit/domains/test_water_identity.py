"""Reviewable station and water-body matches to places and rivers (#2582, WA06)."""

from __future__ import annotations

import pytest

from src.kb.water_identity import WaterIdentity, geometry_relation
from src.kb.water_records import WaterError
from tests.unit.water import fixture_builder
from tests.unit.water import harness as h

NS = h.NS


@pytest.fixture
def world():
    env = h.Env().loaded()
    places = env.places()
    return env, places, WaterIdentity(env.conn, now=env.tick)


def _match(result, subject, place_id, relation, method):
    return next(m for m in result["matches"] if m["subject_key"] == subject and m["place_id"] == place_id
                and m["relation"] == relation and m["method"] == method)


def test_proposals_carry_method_evidence_confidence_and_nothing_is_accepted(world):
    _env, places, identity = world
    result = identity.propose(NS, principal_id="alice", scopes=h.ALL)
    assert result["proposed"] and {m["state"] for m in result["matches"]} == {"proposed"}
    dresden = _match(result, "pegelonline:" + fixture_builder.DRESDEN, places["dresden"], "located-in",
                     "published-coordinates")
    assert dresden["confidence"] == "high" and dresden["evidence"]["receipt_id"].startswith("spatial-receipt:")
    assert dresden["evidence"]["place"]["geometry_version"]["geometry_id"]
    assert dresden["evidence"]["subject"]["revision_id"] == dresden["subject_revision_id"]
    county = _match(result, "usgs:USGS-01646500", places["montgomery"], "located-in", "published-identifier")
    assert county["evidence"]["identifiers"] == [{"scheme": "us-county-fips", "value": "24031"}]
    rivers = [m for m in result["matches"] if m["place_id"] == places["elbe"]]
    assert {m["method"] for m in rivers} == {"published-identifier"} and len(rivers) == 2
    body = _match(result, "eu-wb:DERW_DESN_FIX-0001", places["dresden"], "intersects", "published-geometry")
    assert body["evidence"]["geometry_vintage"] == "WISE WFD 2016 reference spatial dataset"
    assert [u["subject_key"] for u in result["unmatched"]] == ["eu-wb:DERW_DESN_FIX-0002"]
    assert not [m for m in result["matches"] if m["place_id"] == places["nowhere"]]
    again = identity.propose(NS, principal_id="alice", scopes=h.ALL)
    assert again["proposed"] == [] and len(again["matches"]) == len(result["matches"])


def test_river_names_are_used_only_without_an_identifier(world):
    env, places, identity = world
    named = env.place("Elbe", None, place_type="river", source_ids={"wikidata": "Q1644"})["place_id"]
    result = identity.propose(NS, principal_id="alice", scopes=h.ALL)
    by_name = [m for m in result["matches"] if m["place_id"] == named]
    assert {m["method"] for m in by_name} == {"river-name"} and {m["confidence"] for m in by_name} == {"low"}
    assert {m["evidence_class"] for m in by_name} == {"lower-evidence"}
    by_id = [m for m in result["matches"] if m["place_id"] == places["elbe"]]
    assert {m["method"] for m in by_id} == {"published-identifier"}


def test_review_accept_reject_and_revert_record_decisions(world):
    _env, places, identity = world
    result = identity.propose(NS, principal_id="alice", scopes=h.ALL)
    county = _match(result, "usgs:USGS-01646500", places["montgomery"], "located-in", "published-identifier")
    with pytest.raises(WaterError):
        identity.review(NS, county["match_id"], "accept", "", principal_id="bob", scopes=h.ALL)
    with pytest.raises(WaterError):
        identity.review(NS, county["match_id"], "accept", "fips", principal_id="bob", scopes=h.WRITE)
    accepted = identity.review(NS, county["match_id"], "accept", "published county FIPS", principal_id="bob",
                               scopes=h.ALL)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "bob" and accepted["decision_id"]
    assert identity.accepted(NS, place_id=places["montgomery"])[0]["match_id"] == county["match_id"]
    with pytest.raises(WaterError):
        identity.review(NS, county["match_id"], "reject", "twice", principal_id="bob", scopes=h.ALL)
    reverted = identity.revert(NS, county["match_id"], "boundary vintage in doubt", principal_id="carol",
                               scopes=h.ALL)
    assert reverted["state"] == "reverted" and [x["state"] for x in reverted["history"]] == [
        "proposed", "accepted", "reverted"]
    assert identity.accepted(NS, place_id=places["montgomery"]) == []
    body = _match(result, "eu-wb:DERW_DESN_FIX-0001", places["dresden"], "intersects", "published-geometry")
    rejected = identity.review(NS, body["match_id"], "reject", "not this district", principal_id="bob", scopes=h.ALL)
    assert rejected["state"] == "rejected" and identity.generation(NS) == 3


def test_a_revised_station_marks_the_match_as_based_on_an_earlier_revision(world):
    env, places, identity = world
    result = identity.propose(NS, principal_id="alice", scopes=h.ALL)
    dresden = next(m for m in result["matches"] if m["place_id"] == places["dresden"]
                   and m["relation"] == "located-in")
    identity.review(NS, dresden["match_id"], "accept", "inside the boundary", principal_id="bob", scopes=h.ALL)
    env.advance(8)
    assert env.run("water-later", later=True)["status"] == "complete"
    (after,) = identity.matches(NS, scopes=h.ALL, subject_key=dresden["subject_key"], place_id=places["dresden"])
    assert after["state"] == "accepted" and after["subject_revised_since"] is True


def test_geometry_relation_distinguishes_within_crossing_and_outside():
    square = h.DRESDEN
    assert geometry_relation(square, {"type": "LineString", "coordinates": [[13.7, 51.0], [13.8, 51.1]]})[
        "relation"] == "within"
    assert geometry_relation(square, {"type": "LineString", "coordinates": [[13.5, 51.0], [13.8, 51.1]]})[
        "relation"] == "crosses"
    assert geometry_relation(square, {"type": "LineString", "coordinates": [[13.3, 51.2], [13.4, 51.2]]})[
        "relation"] == "outside"
