"""Water records linked to hazards, weather, infrastructure and places by citation and accepted match (#2582, WA07)."""

from __future__ import annotations

from src.kb.water_identity import WaterIdentity
from src.kb.water_links import NO_CAUSATION, WaterLinks, providers
from tests.unit.water import fixture_builder
from tests.unit.water import harness as h

NS = h.NS


def test_links_record_their_basis_and_pin_revisions_on_both_sides():
    env = h.Env().loaded()
    places = env.places()
    identity = WaterIdentity(env.conn, now=env.tick)
    proposed = identity.propose(NS, principal_id="alice", scopes=h.ALL)
    county = next(m for m in proposed["matches"] if m["place_id"] == places["montgomery"]
                  and m["method"] == "published-identifier")
    identity.review(NS, county["match_id"], "accept", "published FIPS", principal_id="bob", scopes=h.ALL)
    hazard = h.seed_hazard(env.conn, cited="990001")
    weather = h.seed_weather_identifier(env.conn, scheme="usgs-monitoring-location", value="USGS-01646500")
    asset = h.seed_infrastructure_asset(env.conn, scheme="eu-water-body", value="DERW_DESN_FIX-0001")

    result = WaterLinks(env.conn, now=env.tick).discover(NS, principal_id="alice", scopes=h.ALL)
    by_kind = {}
    for link in result["links"]:
        by_kind.setdefault(link["target"]["kind"], []).append(link)
    (flood,) = by_kind["hazard-record"]
    assert flood["basis"] == "citation" and flood["target"]["revision"] == hazard["revision_id"]
    assert flood["evidence"]["cited_identifier"] == "990001" and "990001" in flood["evidence"]["quote"]
    station = env.store.find(NS, "station", "pegelonline", fixture_builder.DRESDEN)
    assert flood["record_id"] == station["record_id"]
    assert flood["record_revision_id"] == env.store.current(NS, station["record_id"])["revision_id"]
    (met,) = by_kind["weather-station"]
    assert met["basis"] == "published_relation" and met["target"]["id"] == weather
    (plant,) = by_kind["infrastructure-asset"]
    assert plant["basis"] == "shared_identifier" and plant["target"]["revision"] == asset["revision_id"]
    (place,) = by_kind["place"]
    assert place["basis"] == "accepted_match" and place["evidence"]["match_id"] == county["match_id"]
    assert place["target"]["revision"] == county["place_revision_id"]
    assert all(link["claims"] == NO_CAUSATION for link in result["links"])
    assert "document" not in by_kind  # the runtime's own page documents are not citations
    assert "dam or waterway" in result["providers"]["infrastructure"]["coverage_note"]
    again = WaterLinks(env.conn, now=env.tick).discover(NS, principal_id="alice", scopes=h.ALL)
    assert len(WaterLinks(env.conn).links(NS, scopes=h.ALL)) == len(again["links"]) == len(result["links"])


def test_missing_providers_are_reported_not_dropped():
    env = h.Env().loaded()
    state = providers(env.conn)
    assert {k for k, v in state.items() if v["status"] == "unavailable"} == {
        "natural-hazards", "weather", "infrastructure", "places (accepted matches)"}
    result = WaterLinks(env.conn, now=env.tick).discover(NS, principal_id="alice", scopes=h.ALL)
    assert result["links"] == []
    assert result["unavailable_providers"]["natural-hazards"]["reason"]
    assert result["unavailable_providers"]["weather"]["status"] == "unavailable"
