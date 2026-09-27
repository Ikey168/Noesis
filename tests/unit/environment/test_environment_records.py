"""E02: record model — mandatory kind, units through pint, schemas in the registry."""

import json

import duckdb
import pytest

from src.kb import environment_records as er
from tests.unit.environment import harness

URL = "https://example.invalid.test/"


def _series(provider="uba", kind="observation", **extra):
    fields = dict(source_url="https://www.umweltbundesamt.de/", location={"kind": "station", "ref": "uba:282"},
                  indicator={"code": "NO2"}, unit="µg/m³", interval="PT1H", aggregation="mean", kind=kind,
                  values=[{"start": "2026-09-24T00:00:00Z", "value": "41.3", "status": "provisional"}])
    fields.update(extra)
    return er.series(provider, "282:5:2", "UBA NO2", **fields)


def test_kind_is_mandatory_and_never_defaulted():
    with pytest.raises(er.EnvironmentRecordError) as missing:
        _series(kind=None)
    assert missing.value.code == "kind_required"
    with pytest.raises(er.EnvironmentRecordError) as invalid:
        _series(kind="measured")
    assert invalid.value.code == "invalid_kind"


def test_forecast_and_reanalysis_can_never_be_stored_as_observations():
    for provider in ("open-meteo-archive", "open-meteo-forecast"):
        with pytest.raises(er.EnvironmentRecordError) as error:
            _series(provider=provider, kind="observation", location={"kind": "grid-cell", "ref": "x",
                                                                      "geometry": {"type": "Point", "coordinates": [13.4, 52.5]}})
        assert error.value.code == "kind_not_published"
    with pytest.raises(er.EnvironmentRecordError) as model_on_observation:
        _series(model={"name": "ERA5"})
    assert model_on_observation.value.code == "kind_conflict"
    grid = {"kind": "grid-cell", "ref": "c", "geometry": {"type": "Point", "coordinates": [13.4, 52.5]}}
    with pytest.raises(er.EnvironmentRecordError) as no_issue:
        _series(provider="open-meteo-forecast", kind="forecast", location=grid, model={"name": "ICON-D2"})
    assert no_issue.value.code == "issue_time_required"
    forecast = _series(provider="open-meteo-forecast", kind="forecast", location=grid,
                       model={"name": "ICON-D2", "issue_time": None})
    assert forecast["kind"] == "forecast" and "model.issue_time" in forecast["unknowns"]
    with pytest.raises(er.EnvironmentRecordError):
        er.grid_event("smard", "410", "load", source_url="https://www.smard.de/", event_type="load",
                      bidding_zone={"code": "DE"}, kind="model", unit="MWh")


def test_values_are_exact_decimals_and_missing_values_stay_null():
    with pytest.raises(er.EnvironmentRecordError):
        _series(values=[{"start": "2026-09-24T00:00:00Z", "value": 41.3, "status": "unknown"}])
    record = _series(values=[{"start": "2026-09-24T00:00:00Z", "value": None, "status": "unknown"}])
    assert record["values"][0]["value"] is None
    with pytest.raises(er.EnvironmentRecordError):
        _series(values=[{"start": "2026-09-24T00:00:00Z", "value": "1", "status": "checked"}])


def test_units_carry_through_the_existing_pint_normalisation():
    assert _series()["unit_pint"] == "microgram / meter ** 3"
    assert er.normalise("41.3", "µg/m³", target="mg/m³")["value"] == "0.041300"
    assert er.normalise("285.15", "K")["value"] == "12.000000"
    assert er.normalise("1250", "MWh", target="GWh")["value"] == "1.250000"
    assert er.normalise("7", "allowances") is None  # never guessed
    unmapped = er.series("eu-ets", "DE_1:allocated_allowances", "alloc", source_url="https://climate.ec.europa.eu/x",
                         location={"kind": "facility", "ref": "eu-ets:DE_1"}, indicator={"code": "allocated_allowances"},
                         unit="allowances", interval="P1Y", aggregation="total", kind="observation", values=[])
    assert "unit_unmapped" in unmapped["unknowns"]


def test_unavailability_keeps_published_reason_and_is_never_a_default():
    with pytest.raises(er.EnvironmentRecordError):
        er.grid_event("entsoe", "x", "outage", source_url="https://transparency.entsoe.eu/", event_type="unavailability",
                      bidding_zone={"code": "10Y1001A1001A82H"}, kind="observation", unit="MW")
    event = er.grid_event("entsoe", "x", "outage", source_url="https://transparency.entsoe.eu/", event_type="unavailability",
                          bidding_zone={"code": "10Y1001A1001A82H"}, kind="observation", unit="MW",
                          unavailability={"kind": "unplanned", "start": "2026-09-24T01:40:00Z", "reason": []})
    assert "reason" in event["unknowns"] and event["unavailability"]["source"].startswith("published")


def test_schemas_are_registered_and_fixture_records_validate():
    from src.ingestion.source_packs import replay_native_fixture
    from src.kb.schema_registry import SchemaRegistry

    conn = duckdb.connect()
    scopes = {"knowledge:schema:register", "knowledge:schema:read", "knowledge:schema:validate"}
    modules = er.register_schemas(conn, principal_id="env-service", scopes=scopes)
    assert {m["name"] for m in modules} == {"noesis-environment-record", "noesis-environment-dossier"}
    assert er.register_schemas(conn, principal_id="env-service", scopes=scopes)[0]["idempotent_replay"]
    registry = SchemaRegistry(conn)
    reference = {"kind": "schema", "name": "noesis-environment-record", "version": "1.0.0"}
    manifest = harness.pack_manifest()
    count = 0
    for source in manifest["sources"]:
        fixture = json.loads((harness.ROOT / source["fixture"]["path"]).read_text())
        for item in replay_native_fixture(source, fixture):
            result = registry.validate_instance(reference, item["environment_record"], scopes=scopes)
            assert result["valid"], (source["source_id"], result["errors"])
            count += 1
    assert count >= 25
    broken = {k: v for k, v in _series().items() if k != "kind"}
    assert not registry.validate_instance(reference, broken, scopes=scopes)["valid"]


def test_revalidation_round_trips_every_record_type():
    station = er.station("uba", "282", "Berlin Mitte", source_url=URL, geometry={"type": "Point", "coordinates": [13.4, 52.5]},
                         identifiers={"uba_station_id": "282"})
    facility = er.facility("eea-industry", "F1", "Plant", source_url=URL, geometry=None, operator={"name": None},
                           releases=[{"year": 2024, "pollutant": "CO2", "value": "1", "unit": "kg", "kind": "observation"}])
    assert {"geometry", "operator.name"} <= set(facility["unknowns"])
    for record in (station, facility, _series()):
        assert er.validate(record) == record
