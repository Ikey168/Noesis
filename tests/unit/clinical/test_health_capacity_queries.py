"""Capacity indicators for a place as of a date with definitions, notes, breaks and vintages (HS08)."""

from __future__ import annotations

import json

import pytest

from src.kb.health_capacity import HealthCapacityError
from src.kb.health_capacity_comparability import HealthCapacityComparability
from src.kb.health_capacity_queries import capacity_as_of
from tests.unit.clinical import health_capacity_harness as h

OECD_BEDS = {"provider": "oecd-health", "source_code": "DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC:HOSP_BEDS"}
ESTAT_BEDS = {"provider": "eurostat-health", "source_code": "hlth_rs_bds1:HBEDT"}


@pytest.fixture
def env():
    environment = h.Env()
    environment.acquire("r1")
    environment.places = h.register_places(environment.conn)
    tool = HealthCapacityComparability(environment.conn, now=environment.clock)
    tool.resolve_places(h.NS, principal_id="analyst", scopes=h.SCOPES)
    note = tool.record_note(h.NS, OECD_BEDS, ESTAT_BEDS, "different_unit_or_denominator",
                            "per 1 000 (OECD) against per 100 000 inhabitants (Eurostat)", principal_id="analyst",
                            scopes=h.SCOPES)
    tool.review_note(h.NS, note["note_id"], "accept", "cited", principal_id="reviewer", scopes=h.SCOPES)
    environment.note = note
    return environment


def entries(answer, domain, provider):
    return answer["domains"][domain][provider]


def test_a_place_returns_every_source_side_by_side_with_unit_definition_flags_and_citations(env):
    answer = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_id=env.places["DEU"])
    assert answer["status"] == "answered" and answer["place"]["kind"] == "place"
    assert {c["code"] for c in answer["place"]["codes"]} == {"DEU", "DE"}
    assert set(answer["domains"]["beds"]) == {"who-gho", "oecd-health", "eurostat-health"}
    assert set(answer["domains"]["workforce"]) == {"who-gho", "oecd-health"}
    assert set(answer["domains"]["expenditure"]) == {"who-gho", "eurostat-health"}
    (oecd,) = entries(answer, "beds", "oecd-health")
    assert oecd["unit"] == "per 1000 population" and oecd["indicator_notes"]["country_note"]
    assert oecd["citation"]["source_revision"]["native_revision"] == "OECD.ELS.HD,DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC,1.0"
    assert oecd["values"][-1]["flags"] == ["p: provisional value (OBS_STATUS P)"]
    assert oecd["definitions"][0]["text"] and oecd["values"][0]["definition"]["version"]
    (eurostat,) = entries(answer, "beds", "eurostat-health")
    assert eurostat["unit"] == "per 100 000 population"
    # Comparability notes inline, on both sides.
    assert [n["note_id"] for n in oecd["comparability_notes"]] == [env.note["note_id"]]
    assert [n["note_id"] for n in eurostat["comparability_notes"]] == [env.note["note_id"]]
    # Sources are never blended: every entry is one series of one provider; nothing is averaged or ranked.
    text = json.dumps(answer)
    for forbidden in ('"rank', '"score', '"average', '"combined', '"harmonised'):
        assert forbidden not in text
    assert all(len({e["provider"] for e in items}) == 1
               for providers in answer["domains"].values() for items in providers.values())


def test_definition_breaks_are_inline_and_missing_values_are_unknown(env):
    answer = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_code="DE", domains=["expenditure"])
    hf1 = next(e for e in entries(answer, "expenditure", "eurostat-health") if e["source_code"].endswith("HF1"))
    assert {b["capacity_kind"] for b in hf1["breaks"]} == {"definition-break", "publisher-flagged-break"}
    assert [v["definition"]["version"] for v in hf1["values"]] == ["SHA 1.0 HF.1", "SHA 2011 HF.1"]
    (gho,) = entries(answer, "expenditure", "who-gho")
    assert gho["values"][-1]["status"] == "unknown" and gho["values"][-1]["value"] is None
    assert answer["request"]["domains"] == ["expenditure"] and set(answer["domains"]) == {"expenditure"}


def test_the_as_of_time_selects_the_vintage_then_published_and_the_comparison_cites_both(env):
    env.eurostat_update("hlth_rs_bds1")
    env.acquire("r2", keys=["eurostat"])
    earlier = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_id=env.places["DEU"], as_of="2098-06-30",
                             domains=["beds"])
    (old,) = entries(earlier, "beds", "eurostat-health")
    assert old["values"][-1]["value"] == "782.0" and old["later_vintages"] == 1
    assert old["vintage_differences"] is None
    latest = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_id=env.places["DEU"], domains=["beds"])
    (new,) = entries(latest, "beds", "eurostat-health")
    assert new["values"][-1]["value"] == "783.4"
    differences = new["vintage_differences"]
    assert differences["left"]["vintage_id"] == old["vintage"]["vintage_id"]
    assert differences["right"]["vintage_id"] == new["vintage"]["vintage_id"]
    assert [c["reference_period"] for c in differences["changes"]] == ["2097"]
    # Before any release the series is unavailable, never estimated.
    before = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_id=env.places["DEU"], as_of="2098-01-01")
    assert all(e["status"] == "unavailable" and e["values"] == []
               for providers in before["domains"].values() for items in providers.values() for e in items)


def test_aggregates_answer_as_themselves_and_unknown_places_have_none_on_record(env):
    eu = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_code="EU27_2020")
    assert eu["place"]["kind"] == "aggregate" and eu["status"] == "answered"
    assert all(e["aggregate"] for providers in eu["domains"].values() for items in providers.values() for e in items)
    unknown = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_code="ITA")
    assert unknown["status"] == "none_on_record" and unknown["domains"] == {}
    from src.kb.geospatial import GeospatialStore

    spain = GeospatialStore(env.conn).register_place(
        "global", "Spain (capacity fixture)", "country", names=[{"value": "Spain", "language": "en",
                                                                 "kind": "canonical"}],
        source_ids={"iso3166-1-alpha3": "ESP"}, parent_ids=[], principal_id="operator", scopes=h.GEO_SCOPES)
    none = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_id=spain["place_id"])
    assert none["status"] == "none_on_record" and none["note"] == "no capacity indicator is on record for this place"
    # A place whose every code resolution was rejected is unresolved, not empty.
    tool = HealthCapacityComparability(env.conn, now=env.clock)
    for resolution in tool.place_codes(h.NS, env.places["FRA"]):
        tool.review_place(h.NS, resolution["resolution_id"], "reject", "not France", principal_id="reviewer",
                          scopes=h.SCOPES)
    france = capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_id=env.places["FRA"])
    assert france["status"] == "place_unresolved" and len(france["excluded_resolutions"]) == 2
    with pytest.raises(HealthCapacityError):
        capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_id="x", place_code="DE")
    with pytest.raises(HealthCapacityError):
        capacity_as_of(env.conn, h.NS, scopes=h.READ_ONLY, place_code="DE", domains=["quality"])
