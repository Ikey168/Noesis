"""WA02 (#2593): record contracts, minimisation at write time, revision chains and as-of lookup."""

import json

import duckdb
import pytest

from src.ingestion.water_sources import replay_native_fixture
from src.kb import water_records as wr
from src.kb.water_store import WaterStore
from tests.unit.water import harness

URL = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/daily/items"


def _observation(**over):
    published = {"station_id": "USGS-99990001", "parameter": "discharge", "parameter_code": "00060",
                 "statistic": "00003", "time": "2026-09-03", "value": 13.4, "unit": "ft^3/s",
                 "quality": wr.quality("Provisional", provider="usgs"), **over}
    return wr.statement("observation", "usgs", wr.observation_key("USGS-99990001", "00060", "00003", "2026-09-03"),
                        subject_name=None, source={"url": URL}, as_published=published)


def test_every_record_type_round_trips_through_its_constructor():
    records = []
    for source in harness.manifest()["sources"]:
        fixture = json.loads((harness.ROOT / source["fixture"]["path"]).read_text())
        records += [r["water_record"] for r in replay_native_fixture(source, fixture)]
    assert {r["record_type"] for r in records} == set(wr.RECORD_TYPES)
    for record in records:
        assert wr.validate_statement(record) == record
        assert record["source"]["url"].startswith("https://")


def test_schema_registers_and_validates_every_fixture_statement():
    from src.kb.schema_registry import SchemaRegistry

    conn = duckdb.connect()
    scopes = {"knowledge:schema:register", "knowledge:schema:read", "knowledge:schema:validate"}
    modules = wr.register_schemas(conn, principal_id="water-service", scopes=scopes)
    assert [m["name"] for m in modules] == ["noesis-water-record"]
    assert wr.register_schemas(conn, principal_id="water-service", scopes=scopes)[0]["idempotent_replay"]
    registry = SchemaRegistry(conn)
    reference = {"kind": "schema", "name": "noesis-water-record", "version": "1.0.0"}
    for source in harness.manifest()["sources"]:
        fixture = json.loads((harness.ROOT / source["fixture"]["path"]).read_text())
        for item in replay_native_fixture(source, fixture):
            result = registry.validate_instance(reference, item["water_record"], scopes=scopes)
            assert result["valid"], result["errors"]
    broken = _observation()
    broken["as_published"]["quality"] = {"state": "checked"}
    assert not registry.validate_instance(reference, broken, scopes=scopes)["valid"]


def test_exclusions_and_minimisation_are_enforced_at_write_time():
    for bad in ({"interpolated": True}, {"forecast": 1.0}, {"flood_risk": "high"}):
        with pytest.raises(wr.WaterError) as error:
            _observation(**bad)
        assert error.value.code == "forbidden_field"
    with pytest.raises(wr.WaterError) as personal:
        wr.statement("station", "usgs", "USGS-1", subject_name="x", source={"url": URL},
                     as_published={"native_id": "USGS-1", "name": "x", "location": {"latitude": 1.0,
                                                                                    "longitude": 1.0},
                                   "agency": "USGS", "contact": {"email": "someone@example.org"}})
    assert personal.value.code == "personal_field"
    with pytest.raises(wr.WaterError):
        wr.statement("station", "usgs", "USGS-1", subject_name="x", source={"url": URL, "email": "a@example.org"},
                     as_published={"native_id": "USGS-1", "name": "x", "location": {}})
    assert wr.MINIMISATION["decision"] == "no-personal-data" and "email" in wr.MINIMISATION["excluded"]
    record = _observation()
    assert "qualifiers" in record["unknowns"] and record["as_published"]["quality"]["state"] == "provisional"
    assert wr.quality(None, provider="pegelonline")["state"] == "provisional"
    assert wr.quality("Approved", provider="usgs")["state"] == "approved"
    assert wr.quality("Estimated?", provider="usgs")["state"] == "unknown"


def test_assessments_keep_cycles_and_elements_as_reported():
    base = {"eu_code": "DEFX_1", "reporting_cycle": "WFD reporting 2016", "cycle_year": "2016",
            "ecological": {"value": "3", "label": "Moderate", "kind": "status"}}
    record = wr.statement("assessment", "eea-wise", "DEFX_1|2016", subject_name=None,
                          source={"url": "https://discodata.eea.europa.eu/sql"}, as_published=base)
    assert record["subject"]["key"] == "wfd:DEFX_1" and "chemical" in record["unknowns"]
    for bad in ({"cycle_year": "2016-2021"}, {"ecological": {"label": "Moderate"}}, {"merged_status": "moderate"}):
        with pytest.raises(wr.WaterError):
            wr.statement("assessment", "eea-wise", "DEFX_1|2016", subject_name=None,
                         source={"url": "https://discodata.eea.europa.eu/sql"}, as_published={**base, **bad})


def test_revision_chains_are_immutable_and_as_of_returns_what_was_on_record():
    clock = [1_000]
    store = WaterStore(duckdb.connect(), now=lambda: clock[0])
    first = store.observe("environment", [_observation()], observed_at_ms=1_000)
    assert first["counts"] == {"created": 1, "revised": 0, "unchanged": 0}
    assert store.observe("environment", [_observation()], observed_at_ms=2_000)["counts"]["unchanged"] == 1
    approved = _observation(quality=wr.quality("Approved", provider="usgs"), value=13.5)
    second = store.observe("environment", [approved], observed_at_ms=3_000)
    assert second["counts"]["revised"] == 1
    record_id = second["results"][0]["record_id"]
    chain = store.revisions("environment", record_id)
    assert [r["revision_no"] for r in chain] == [1, 2] and chain[1]["supersedes"] == chain[0]["revision_id"]
    assert [r["statement"]["as_published"]["quality"]["state"] for r in chain] == ["provisional", "approved"]
    assert store.current("environment", record_id, as_of_ms=2_500)["revision_id"] == chain[0]["revision_id"]
    assert store.current("environment", record_id, as_of_ms=500) is None
    assert store.current("environment", record_id)["revision_id"] == chain[1]["revision_id"]


def test_station_location_and_gauge_zero_changes_are_vintages():
    env = harness.Env().loaded()
    env.advance(7)
    assert env.run("water-later", later=True)["status"] == "complete"
    exampla = env.store.find(harness.NS, "station", "pegelonline", "aaaa1111-0000-4000-8000-00000000e001")
    vintages = env.store.station_vintages(harness.NS, exampla["record_id"])
    assert [d["datum"]["gauge_zero"]["valid_from"] for d in vintages["datums"]] == ["2019-11-01", "2026-09-25"]
    assert len(vintages["locations"]) == 1
    northwind = env.store.find(harness.NS, "station", "pegelonline", "aaaa1111-0000-4000-8000-00000000e002")
    moved = env.store.station_vintages(harness.NS, northwind["record_id"])["locations"]
    assert [v["location"]["latitude"] for v in moved] == [52.40, 52.401]
