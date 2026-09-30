"""EN10 (#2259): generation mix, load, price and capacity as-of answers with revision history."""

from __future__ import annotations

import duckdb
import pytest

from src.kb.energy_identity import EnergyIdentity
from src.kb.energy_queries import EnergyQueries
from src.kb.energy_store import EnergyStore, ms
from tests.unit.energy.harness import NS, SCOPES, acquire, acquire_all

ZONE = "10Y1001A1001A82H"


@pytest.fixture(scope="module")
def world():
    conn = duckdb.connect()
    acquire_all(conn)
    acquire(conn, "entsoe_revision_2")
    acquire(conn, "ember_generation_release_2")
    identity = EnergyIdentity(conn)
    for match in identity.propose(NS, principal_id="analyst", scopes=SCOPES)["proposed"]:
        identity.review(NS, match["match_id"], "accept", "checked", principal_id="reviewer", scopes=SCOPES)
    return conn, EnergyQueries(conn)


def test_generation_mix_keeps_sources_side_by_side_with_citations(world):
    _, queries = world
    answer = queries.generation_mix(NS, "DEU", scopes=SCOPES)
    assert answer["status"] == "answered"
    assert [s["source"] for s in answer["sources"]] == ["ember:ember:electricity-generation/monthly",
                                                         "energy-charts:energy-charts:public_power"]
    ember = answer["sources"][0]
    assert ember["fuels"] == ["Solar", "Total generation", "Wind"] and len(ember["publisher_aggregates"]) == 1
    charts = answer["sources"][1]["series"][0]
    assert charts["derived_from"]["provider"] == "entsoe" and charts["reached_through"]["match"]["state"] == "accepted"
    assert [r["subject"]["code"] for r in answer["related_subjects"]] == [ZONE]
    zone = queries.generation_mix(NS, ZONE, scopes=SCOPES, start="2026-09-24T00:00:00Z", end="2026-09-24T02:00:00Z")
    series = zone["sources"][0]["series"]
    assert {s["facets"]["fuel"]["code"] for s in series} == {"B16", "B19"}
    assert all(len(s["values"]) == 2 for s in series)
    citation = series[0]["citation"]
    for key in ("provider", "dataset", "source_url", "attribution", "licence", "release", "status", "retrieved_at",
                "receipt"):
        assert citation[key] is not None, key


def test_as_of_picks_the_vintage_published_at_or_before_it(world):
    _, queries = world
    before_revision = queries.generation_mix(NS, ZONE, scopes=SCOPES, as_of_ms=ms("2026-09-25T00:00:00Z"))
    after_revision = queries.generation_mix(NS, ZONE, scopes=SCOPES, as_of_ms=ms("2026-09-27T00:00:00Z"))
    wind_before = next(s for s in before_revision["sources"][0]["series"] if s["facets"]["fuel"]["code"] == "B19")
    wind_after = next(s for s in after_revision["sources"][0]["series"] if s["facets"]["fuel"]["code"] == "B19")
    assert wind_before["publication_status"] == "provisional" and wind_before["values"][1]["value"] == "9204"
    assert wind_before["later_vintages_after_as_of"] == [wind_after["vintage_id"]]
    assert wind_after["publication_status"] == "revised" and wind_after["values"][1]["value"] == "9240"
    too_early = queries.generation_mix(NS, ZONE, scopes=SCOPES, as_of_ms=ms("2026-09-01T00:00:00Z"))
    assert {s["status"] for s in too_early["sources"][0]["series"]} == {"not_published_by_as_of"}
    ember_aug = queries.generation_mix(NS, "DEU", scopes=SCOPES, as_of_ms=ms("2026-09-01T00:00:00Z"))
    solar = next(s for s in ember_aug["sources"][0]["series"] if s["facets"]["fuel"]["code"] == "Solar")
    assert solar["citation"]["release"]["label"] == "Ember monthly electricity data 2026-08"


def test_revision_history_lists_each_vintage_with_release_and_change(world):
    conn, queries = world
    wind = next(s for s in EnergyStore(conn).series(NS, scopes=SCOPES, provider="entsoe", record_type="generation")
                if s["facets"]["fuel"]["code"] == "B19")
    history = queries.revision_history(NS, wind["series_id"], scopes=SCOPES, period_start="2026-09-24T01:00:00Z")
    assert [v["status"] for v in history["vintages"]] == ["provisional", "revised"]
    assert [v["figure"]["value"] for v in history["vintages"]] == ["9204", "9240"]
    assert [v["published_at"] for v in history["vintages"]] == ["2026-09-24T05:02:11Z", "2026-09-26T09:40:00Z"]
    change = history["vintages"][1]["changes_from_previous"]
    assert change == [{"key": "2026-09-24T01:00:00Z", "change": "value_revised", "delta": "36", "unit": "MW",
                       "left": {"value": "9204", "status": "provisional", "vintage_id": history["vintages"][0]["vintage_id"]},
                       "right": {"value": "9240", "status": "revised", "vintage_id": history["vintages"][1]["vintage_id"]}}]
    assert history["vintages"][1]["citation"]["release"]["revision"] == "2"


def test_load_price_and_flows_answer_per_source(world):
    _, queries = world
    load = queries.observations(NS, ZONE, {"load"}, scopes=SCOPES)
    assert [s["source"] for s in load["sources"]] == ["entsoe:entsoe:A65:A16"]
    price = queries.observations(NS, ZONE, {"price"}, scopes=SCOPES)["sources"][0]["series"][0]
    assert price["unit"] == "EUR/MWh" and price["values"][2]["value"] == "-3.50"
    flows = queries.observations(NS, "CISO", {"cross_border_flow"}, scopes=SCOPES)["sources"][0]["series"]
    assert {s["counterpart"]["code"] for s in flows} == {"AZPS", "BPAT"}


def test_capacity_as_of_by_plant_unit_and_zone_with_unknowns(world):
    _, queries = world
    plant = queries.capacity_as_of(NS, "ent-eia-plant-99901", "2026-06-15", scopes=SCOPES)
    by_unit = {c["subject"]["code"]: c for c in plant["capacity"]}
    assert by_unit["99901:G1"]["capacity_on_date"] == "248" and by_unit["99901:G1"]["state"] == "known"
    assert by_unit["99901:G2"]["capacity_on_date"] == "50"
    july = {c["subject"]["code"]: c for c in queries.capacity_as_of(NS, "ent-eia-plant-99901", "2026-07-15",
                                                                     scopes=SCOPES)["capacity"]}
    assert july["99901:G2"]["state"] == "not_in_effect" and july["99901:G2"]["capacity_on_date"] is None
    later = {c["subject"]["code"]: c for c in queries.capacity_as_of(NS, "ent-eia-plant-99901", "2026-09-01",
                                                                     scopes=SCOPES)["capacity"]}
    assert later["99901:G1"]["state"] == "unknown" and "no published value" in later["99901:G1"]["reason"]
    zone = queries.capacity_as_of(NS, ZONE, "2026-06-01", scopes=SCOPES)
    assert {c["level"] for c in zone["capacity"]} == {"zone"}
    assert sorted(c["capacity_on_date"] for c in zone["capacity"]) == ["63120", "99450"]
    unit = queries.capacity_as_of(NS, "11WD2FIXTURE0001", "2026-06-01", scopes=SCOPES)
    assert unit["capacity"][0]["capacity_on_date"] == "1400" and unit["capacity"][0]["level"] == "unit"


def test_a_subject_with_nothing_on_record_says_so(world):
    _, queries = world
    answer = queries.generation_mix(NS, "10YPL-AREA-----S", scopes=SCOPES)
    assert answer["status"] == "none_on_record" and "no energy records on record" in answer["message"]
    assert queries.capacity_as_of(NS, "ent-unknown-plant", "2026-01-01", scopes=SCOPES)["status"] == "none_on_record"
