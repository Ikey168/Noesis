"""Water records and store: statements, minimisation at write time, revision chains and as-of lookup (#2582, WA02)."""

from __future__ import annotations

import duckdb
import pytest

from src.kb import water_records as wr
from src.kb.water_store import WaterStore

NS = "environment"
SOURCE = {"url": "https://api.waterdata.usgs.gov/ogcapi/v0/collections/continuous/items", "evidence_origin": "fixture"}
STATION = "usgs:USGS-01646500"


def observation(value="4210", state="Provisional", qualifiers=None, time="2026-09-20T12:00:00+00:00"):
    return wr.statement(
        "observation", "usgs", wr.observation_key(STATION, "00060", time), subject_name=None, source=SOURCE,
        as_published={"station_key": STATION, "parameter": "00060", "quantity": "discharge", "unit": "ft^3/s",
                      "time": time, "value": value, "qualifiers": qualifiers,
                      "quality": wr.quality(state, state, basis="USGS approval_status of this value")})


def station(zero="102.73", valid_from="2019-11-01", lat=51.054):
    return wr.statement(
        "station", "pegelonline", "aaaaaaaa-0001-4000-8000-00000000d001", subject_name="DRESDEN",
        source={"url": "https://www.pegelonline.wsv.de/webservices/rest-api/v2/stations/x.json"},
        as_published={"native_id": "aaaaaaaa-0001-4000-8000-00000000d001", "number": "990001", "name": "DRESDEN",
                      "location": {"latitude": lat, "longitude": 13.738, "crs": "WGS84"},
                      "datums": [{"kind": "gauge-zero", "series": "W", "value": zero, "unit": "m. ü. NHN",
                                  "reference": "m. ü. NHN", "valid_from": valid_from}]})


def test_statements_keep_what_was_published_and_list_unknowns():
    value = observation(qualifiers=["Estimated"])
    assert value["contract"] == wr.CONTRACT and value["subject"]["key"] == STATION
    assert value["as_published"]["quality"] == {"state": "provisional", "published": "Provisional",
                                                "basis": "USGS approval_status of this value"}
    assert "statistic" in value["unknowns"] and value["as_published"]["value"] == "4210"
    assert wr.validate_statement(value) == value
    assert wr.decimal_text(212.0) == "212.0" and wr.decimal_text(None) is None
    assert wr.quality("Approved", "Approved", basis="x")["state"] == "approved"
    assert wr.quality("Revised", "Revised", basis="x")["state"] == "unknown"


@pytest.mark.parametrize("field, code", [("forecast", "forbidden_field"), ("interpolated", "forbidden_field"),
                                         ("flood_risk", "forbidden_field"), ("derived_status", "forbidden_field"),
                                         ("email", "personal_field"), ("observer_name", "personal_field")])
def test_forecasts_filled_values_risk_scores_and_personal_fields_are_refused_at_write_time(field, code):
    published = dict(observation()["as_published"])
    published[field] = "x"
    with pytest.raises(wr.WaterError) as error:
        wr.statement("observation", "usgs", "k", subject_name=None, source=SOURCE, as_published=published)
    assert error.value.code == code
    with pytest.raises(wr.WaterError):
        wr.statement("observation", "usgs", "k", subject_name=None, source={**SOURCE, "contact_email": "x"},
                     as_published=observation()["as_published"])
    assert wr.minimise({"a": 1, "email": "x", "nested": [{"phone": "y", "b": 2}]}) == {"a": 1, "nested": [{"b": 2}]}


def test_invalid_statements_fail_closed():
    with pytest.raises(wr.WaterError):
        wr.statement("water_body_assessment", "usgs", "x|2016", subject_name=None, source=SOURCE,
                     as_published={"eu_code": "X", "cycle": "2016", "status_elements": [{"element": "e"}]})
    with pytest.raises(wr.WaterError):
        wr.statement("water_body_assessment", "eea-wise", "X|2016", subject_name=None, source=SOURCE,
                     as_published={"eu_code": "X", "cycle": "2nd", "status_elements": [
                         {"element": "chemical_status", "value": "Good", "published_field": "f"}]})
    with pytest.raises(wr.WaterError):
        wr.statement("station", "pegelonline", "s", subject_name=None, source=SOURCE,
                     as_published={"native_id": "s", "name": "S", "location": {"latitude": 99, "longitude": 1,
                                                                               "crs": "WGS84"}})
    with pytest.raises(wr.WaterError):
        wr.statement("observation", "usgs", "k", subject_name=None, source={"url": "http://insecure"},
                     as_published=observation()["as_published"])
    with pytest.raises(wr.WaterError):
        wr.decimal_text("NaN")


def test_provisional_then_approved_is_a_revision_chain_with_as_of_lookup_and_replays_add_nothing():
    store = WaterStore(duckdb.connect(":memory:"))
    first = store.observe(NS, [observation()], observed_at_ms=1_000)
    assert first["counts"] == {"created": 1, "revised": 0, "unchanged": 0}
    assert store.observe(NS, [observation()], observed_at_ms=2_000)["counts"]["unchanged"] == 1
    approved = store.observe(NS, [observation("4200", "Approved")], observed_at_ms=3_000)["results"][0]
    assert approved["status"] == "revised" and approved["changes"] == ["quality", "value"]
    rid = approved["record_id"]
    chain = store.revisions(NS, rid)
    assert [r["revision_no"] for r in chain] == [1, 2] and chain[1]["supersedes"] == chain[0]["revision_id"]
    assert store.current(NS, rid, as_of_ms=2_500)["statement"]["as_published"]["quality"]["state"] == "provisional"
    assert store.current(NS, rid, as_of_ms=3_000)["statement"]["as_published"]["value"] == "4200"
    assert store.current(NS, rid, as_of_ms=500) is None


def test_station_datum_and_location_changes_are_revisions_and_withdrawals_are_never_deletions():
    store = WaterStore(duckdb.connect(":memory:"))
    store.observe(NS, [station()], observed_at_ms=1_000)
    datum = store.observe(NS, [station(zero="102.70", valid_from="2026-09-25")], observed_at_ms=2_000)["results"][0]
    moved = store.observe(NS, [station(zero="102.70", valid_from="2026-09-25", lat=51.055)],
                          observed_at_ms=3_000)["results"][0]
    assert datum["changes"] == ["datum"] and moved["changes"] == ["location"]
    removed = store.withdraw(NS, "pegelonline", "station", "aaaaaaaa-0001-4000-8000-00000000d001",
                             basis="no longer served", run_id="r", source_id="s", observed_at_ms=4_000)
    assert removed["changes"] == ["removed"]
    assert store.withdraw(NS, "pegelonline", "station", "aaaaaaaa-0001-4000-8000-00000000d001", basis="again",
                          run_id="r", source_id="s") is None
    assert store.withdraw(NS, "pegelonline", "station", "never-seen", basis="x", run_id="r", source_id="s") is None
    chain = store.revisions(NS, removed["record_id"])
    assert [r["event"] for r in chain] == ["published", "published", "published", "removed"]
    assert chain[0]["statement"]["as_published"]["datums"][0]["value"] == "102.73"
    assert store.current(NS, removed["record_id"], as_of_ms=2_500)["statement"]["as_published"]["datums"][0][
        "valid_from"] == "2026-09-25"
