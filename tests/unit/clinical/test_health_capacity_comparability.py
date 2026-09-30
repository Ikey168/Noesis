"""Place resolution, reviewable indicator mappings and comparability notes (HS06); links by citation (HS07)."""

from __future__ import annotations

import pytest

from src.kb.health_capacity import HealthCapacityError
from src.kb.health_capacity_comparability import HealthCapacityComparability
from src.kb.health_capacity_links import HealthCapacityLinks
from src.kb.surveillance import SurveillanceError
from tests.unit.clinical import health_capacity_harness as h
from tests.unit.clinical import surveillance_harness as sh

GHO_BEDS = {"provider": "who-gho", "source_code": "WHS6_102"}
OECD_BEDS = {"provider": "oecd-health", "source_code": "DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC:HOSP_BEDS"}
ESTAT_BEDS = {"provider": "eurostat-health", "source_code": "hlth_rs_bds1:HBEDT"}
GHO_DOCTORS = {"provider": "who-gho", "source_code": "HWF_0001"}
OECD_DOCTORS = {"provider": "oecd-health", "source_code": "DSD_HEALTH_EMP_REAC@DF_PHYS:PRACT_PHYS"}


@pytest.fixture
def env():
    environment = h.Env()
    assert environment.acquire("r1")["status"] == "complete"
    environment.places = h.register_places(environment.conn)
    return environment


def comparability(env):
    return HealthCapacityComparability(env.conn, now=env.clock)


def test_codes_resolve_to_places_by_published_code_and_aggregates_are_never_countries(env):
    result = comparability(env).resolve_places(h.NS, principal_id="analyst", scopes=h.SCOPES)
    views = {(r["geography_system"], r["geography_code"]): r
             for r in comparability(env).resolutions(h.NS, scopes=h.READ_ONLY)}
    assert views[("iso3166-1-alpha3", "DEU")]["place_id"] == env.places["DEU"]
    assert views[("eu-country", "DE")]["place_id"] == env.places["DEU"]
    assert views[("iso3166-1-alpha3", "FRA")]["place_id"] == views[("eu-country", "FR")]["place_id"] == \
        env.places["FRA"]
    for key in (("who-region", "EUR"), ("eurostat-aggregate", "EU27_2020"), ("oecd-aggregate", "OECD")):
        assert views[key]["state"] == "aggregate" and views[key]["place_id"] is None and not views[key]["used"]
    assert len(result["aggregate"]) == 3 and len(result["matched"]) == 4 and not result["unresolved"]
    # Idempotent: a second evaluation that says the same adds nothing.
    again = comparability(env).resolve_places(h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert sorted(again["matched"]) == sorted(result["matched"])
    assert env.conn.execute("SELECT count(*) FROM health_capacity_place_resolutions").fetchone()[0] == 7
    with pytest.raises(SurveillanceError):
        comparability(env).resolve_places(h.NS, principal_id="analyst", scopes=h.SCOPES - {"knowledge:geospatial:read"})


def test_a_code_without_a_place_stays_unresolved_with_the_reason(env):
    env.conn.execute("DELETE FROM geospatial_place_current WHERE place_id=?", [env.places["FRA"]])
    comparability(env).resolve_places(h.NS, principal_id="analyst", scopes=h.SCOPES)
    views = {(r["geography_system"], r["geography_code"]): r
             for r in comparability(env).resolutions(h.NS, scopes=h.READ_ONLY)}
    assert views[("iso3166-1-alpha3", "FRA")]["state"] == "unresolved"
    assert views[("iso3166-1-alpha3", "FRA")]["reason"] == "no place carries this code"


def test_a_place_resolution_is_reviewable_rejectable_and_revertible(env):
    tool = comparability(env)
    tool.resolve_places(h.NS, principal_id="analyst", scopes=h.SCOPES)
    fra = next(r for r in tool.resolutions(h.NS, scopes=h.READ_ONLY) if r["geography_code"] == "FRA")
    rejected = tool.review_place(h.NS, fra["resolution_id"], "reject", "wrong place", principal_id="reviewer",
                                 scopes=h.SCOPES)
    assert rejected["review_state"] == "rejected" and not rejected["used"]
    assert fra["resolution_id"] not in {r["resolution_id"] for r in tool.place_codes(h.NS, env.places["FRA"])}
    reverted = tool.review_place(h.NS, fra["resolution_id"], "revert", "rejected by mistake", principal_id="reviewer",
                                 scopes=h.SCOPES)
    assert reverted["review_state"] == "proposed" and reverted["used"]
    assert [s["state"] for s in reverted["history"]] == ["proposed", "rejected", "proposed"]
    with pytest.raises(SurveillanceError):
        tool.review_place(h.NS, fra["resolution_id"], "accept", "ok", principal_id="reader", scopes=h.READ_ONLY)


def test_an_equivalent_mapping_cites_both_definitions_and_is_accepted(env):
    tool = comparability(env)
    mapping = tool.propose_mapping(h.NS, GHO_BEDS, ESTAT_BEDS, "equivalent",
                                   "both count staffed inpatient beds in all hospitals (definitions cited)",
                                   principal_id="analyst", scopes=h.SCOPES)
    assert mapping["state"] == "proposed" and mapping["kind"] == "equivalent"
    assert {c["provider"] for c in mapping["cited"]} == {"who-gho", "eurostat-health"}
    assert all(c["definition"]["text"] and c["source_revision"]["release_id"] for c in mapping["cited"])
    again = tool.propose_mapping(h.NS, GHO_BEDS, ESTAT_BEDS, "equivalent",
                                 "both count staffed inpatient beds in all hospitals (definitions cited)",
                                 principal_id="analyst", scopes=h.SCOPES)
    assert again["mapping_id"] == mapping["mapping_id"]
    accepted = tool.review_mapping(h.NS, mapping["mapping_id"], "accept", "definitions match",
                                   principal_id="reviewer", scopes=h.SCOPES)
    assert accepted["state"] == "accepted"
    assert [m["mapping_id"] for m in tool.mappings(h.NS, scopes=h.READ_ONLY, ref=ESTAT_BEDS)] == \
        [mapping["mapping_id"]]
    with pytest.raises(HealthCapacityError):
        tool.propose_mapping(h.NS, GHO_BEDS, ESTAT_BEDS, "same", "x", principal_id="a", scopes=h.SCOPES)
    with pytest.raises(HealthCapacityError):
        tool.propose_mapping(h.NS, GHO_BEDS, {"provider": "who-gho", "source_code": "NOPE"}, "equivalent", "x",
                             principal_id="a", scopes=h.SCOPES)


def test_a_definition_difference_note_cites_both_definitions_and_is_reviewable(env):
    tool = comparability(env)
    note = tool.record_note(h.NS, OECD_BEDS, ESTAT_BEDS, "same_concept_different_definition",
                            "OECD excludes day-care beds for France from 2097; Eurostat counts per 100 000, the OECD "
                            "per 1 000", principal_id="analyst", scopes=h.SCOPES)
    assert note["record_type"] == "comparability-note" and note["state"] == "proposed"
    cited = {c["provider"]: c for c in note["cited"]}
    assert cited["oecd-health"]["definition"]["version"] == "OECD Health Statistics 2090"
    assert cited["eurostat-health"]["unit"] == "per 100 000 population"
    accepted = tool.review_note(h.NS, note["note_id"], "accept", "cited", principal_id="reviewer", scopes=h.SCOPES)
    assert accepted["state"] == "accepted"
    reverted = tool.review_note(h.NS, note["note_id"], "revert", "superseded", principal_id="reviewer",
                                scopes=h.SCOPES)
    assert reverted["state"] == "reverted"
    assert tool.notes(h.NS, scopes=h.READ_ONLY, active_only=True) == []


def test_a_rejected_mapping_is_kept_with_its_decision_and_can_be_reverted(env):
    tool = comparability(env)
    mapping = tool.propose_mapping(h.NS, GHO_DOCTORS, OECD_DOCTORS, "broader",
                                   "medical doctors include non-practising physicians", principal_id="analyst",
                                   scopes=h.SCOPES)
    rejected = tool.review_mapping(h.NS, mapping["mapping_id"], "reject", "the GHO series is practising doctors",
                                   principal_id="reviewer", scopes=h.SCOPES)
    assert rejected["state"] == "rejected"
    assert tool.mappings(h.NS, scopes=h.READ_ONLY, active_only=True) == []
    assert [m["state"] for m in tool.mappings(h.NS, scopes=h.READ_ONLY)] == ["rejected"]
    with pytest.raises(HealthCapacityError):
        tool.review_mapping(h.NS, mapping["mapping_id"], "accept", "again", principal_id="r", scopes=h.SCOPES)
    reverted = tool.review_mapping(h.NS, mapping["mapping_id"], "revert", "reopened", principal_id="reviewer",
                                   scopes=h.SCOPES)
    assert [s["state"] for s in reverted["history"]] == ["proposed", "rejected", "reverted"]
    # No value is harmonised or changed by any of this.
    assert not {r[0] for r in env.conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
                if "harmon" in r[0] or "adjusted" in r[0]}


# ---------------------------------------------------------------------- HS07


def test_capacity_is_shown_beside_surveillance_for_the_same_resolved_place_without_a_combined_metric(env):
    sh.Env.acquire(env, "s1", keys=["gho", "eurostat"])  # the pack's surveillance series (tuberculosis)
    links = HealthCapacityLinks(env.conn, now=env.clock)
    links.comparability.resolve_places(h.NS, principal_id="analyst", scopes=h.SCOPES)
    germany = links.beside_surveillance(h.NS, env.places["DEU"], scopes=h.READ_ONLY)
    assert germany["status"] == "resolved"
    assert {c["indicator"]["code"] for c in germany["capacity"]} >= {"WHS6_102", "hlth_rs_bds1:HBEDT"}
    assert {s["condition"]["scheme"] for s in germany["surveillance"]} == {"gho-indicator", "eurostat-icd10"}
    assert all(s["place"]["code"] in {"DEU", "DE"} for s in germany["surveillance"] + germany["capacity"])
    assert not {k for item in germany["capacity"] + germany["surveillance"] for k in item
                if "combined" in k or "per_bed" in k or "rate" in k}


def test_a_rejected_place_resolution_removes_its_series_from_the_place(env):
    links = HealthCapacityLinks(env.conn, now=env.clock)
    tool = links.comparability
    tool.resolve_places(h.NS, principal_id="analyst", scopes=h.SCOPES)
    fra = next(r for r in tool.resolutions(h.NS, scopes=h.READ_ONLY) if r["geography_code"] == "FRA")
    tool.review_place(h.NS, fra["resolution_id"], "reject", "not France", principal_id="reviewer", scopes=h.SCOPES)
    france = links.beside_surveillance(h.NS, env.places["FRA"], scopes=h.READ_ONLY)
    assert {c["place"]["code"] for c in france["capacity"]} == {"FR"}
    assert france["excluded"] == [{"resolution_id": fra["resolution_id"], "geography_system": "iso3166-1-alpha3",
                                   "geography_code": "FRA", "review_state": "rejected"}]
    assert links.beside_surveillance(h.NS, "place:unknown", scopes=h.READ_ONLY)["status"] == "no_resolved_codes"


def test_economics_links_only_where_the_publisher_cites_the_denominator(env):
    from services.ingest.common.series_model import SeriesRecord
    from src.domains.economic.model import register_series

    links = HealthCapacityLinks(env.conn, now=env.clock)
    # Before Economics holds the cited series: the citation is kept as cited-not-held.
    first = links.link_economics(h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert len(first["cited_not_held"]) == 3 and not first["linked"]  # hlth_rs_bds1 cites demo_pjan (DE, FR, EU27)
    register_series(
        env.conn,
        SeriesRecord(series_id="eurostat:demo_pjan:DE", provider="eurostat", title="Population on 1 January",
                     frequency="annual", unit="persons", geography="DE", as_of=1000,
                     observations=[{"period": "2097", "value": 1.0}], metadata={"acquired_at_ms": 2000}),
        semantics={"indicator_id": "indicator:population", "provider_code": "demo_pjan", "scaling": 1},
    )
    second = links.link_economics(h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert len(second["linked"]) == 3
    germany = h.Env.indicator(env, "eurostat-health", "hlth_rs_bds1:HBEDT", "DE")
    cited = links.series_links(h.NS, germany["series_id"], scopes=h.READ_ONLY)
    assert cited["status"] == "linked"
    link = cited["links"][-1]
    assert link["economic_series"][0]["series_id"] == "eurostat:demo_pjan:DE"
    assert link["citation"]["provider_code"] == "demo_pjan" and "hlth_res_esms" in link["locator"]
    # Absent: a series whose publisher cites no denominator series has no link.
    beds = h.Env.indicator(env, "who-gho", "WHS6_102", "DEU")
    assert links.series_links(h.NS, beds["series_id"], scopes=h.READ_ONLY) == {
        "series_id": beds["series_id"], "status": "no_denominator_citation", "links": []}
    doctors = h.Env.indicator(env, "who-gho", "HWF_0001", "DEU")
    assert doctors["series_id"] in second["without_citation"]  # a denominator text without a cited series
    assert links.link_economics(h.NS, principal_id="analyst", scopes=h.SCOPES)["linked"] == second["linked"]
