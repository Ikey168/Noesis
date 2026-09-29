"""E01 contracts and E03/E04/E05/E07 adapters: fail-closed parsers, kinds, units and receipts."""

import json

import pytest

from src.ingestion import environment_providers as ep
from src.ingestion.provider_execution import ProviderError
from tests.unit.environment import fixture_builder, harness

REQUIRED = {"documentation", "access", "authentication", "terms", "rate_limits", "pagination", "cadence",
            "retained_evidence", "publishes", "identifiers", "units", "crs", "coverage", "unavailable_fallback"}
AUDITED = {"openaq", "uba", "entsoe", "smard", "eea-industry", "eu-ets", "umweltatlas", "open-meteo-archive",
           "open-meteo-forecast", "dwd", "copernicus-cams"}


def _parse(provider, selection=None, index=None, overrides=None):
    steps = ep.plan(provider, selection or harness.selections()[provider])
    carry, records = {}, []
    for position, step in enumerate(steps):
        content, _ = fixture_builder.route(provider, step, overrides)
        parsed, carry = ep.parse_step(step, content, carry)
        if index is None or index == position:
            records += parsed
    return records, carry


def test_every_audited_source_has_a_complete_access_contract():
    assert set(ep.PROVIDER_CONTRACTS) == AUDITED
    for provider, contract in ep.PROVIDER_CONTRACTS.items():
        if provider == "copernicus-cams":
            assert contract["status"] == "not implemented" and "account" in contract["reason"]
            continue
        assert REQUIRED <= set(contract), (provider, REQUIRED - set(contract))
        assert set(contract["publishes"]) <= {"observation", "model", "forecast", "features"}
    assert ep.PROVIDER_CONTRACTS["open-meteo-archive"]["publishes"] == {
        "model": "reanalysis (ERA5 family) — never an observation"}
    assert "required" in ep.PROVIDER_CONTRACTS["openaq"]["authentication"]
    assert "securityToken" in ep.PROVIDER_CONTRACTS["entsoe"]["authentication"]


def test_live_state_is_never_assumed():
    assert set(ep.LIVE_VERIFICATION) == AUDITED
    for provider, state in ep.LIVE_VERIFICATION.items():
        assert state["status"] in {"unverified-live", "blocked", "not implemented"}, provider
    assert ep.LIVE_VERIFICATION["copernicus-cams"]["status"] == "not implemented"
    for provider in ep.SECRETS:  # credentialed providers stay unverified until a dated successful run
        assert ep.LIVE_VERIFICATION[provider]["status"] != "verified"


def test_selections_are_bounded_and_unimplemented_sources_refuse():
    with pytest.raises(ProviderError) as cams:
        ep.plan("copernicus-cams", {})
    assert cams.value.code == "not_implemented"
    with pytest.raises(ProviderError):
        ep.plan("openaq", {"locations": [], "sensors": ["1"]})
    with pytest.raises(ProviderError):
        ep.plan("uba", {**harness.selections()["uba"], "data_status": None})
    with pytest.raises(ProviderError):
        ep.plan("entsoe", {**harness.selections()["entsoe"], "bidding_zone": "DE"})
    with pytest.raises(ProviderError):
        ep.plan("smard", {"files": [{"filter": "9999", "region": "DE", "resolution": "hour", "timestamp": 1}]})
    with pytest.raises(ProviderError):
        ep.plan("eu-ets", {"export_url": "https://elsewhere.example/x.csv", "installations": ["DE_1"], "year": 2025})
    with pytest.raises(ProviderError):
        ep.plan("open-meteo-archive", {**harness.selections()["open-meteo-archive"], "model": "icon_d2"})


def test_openaq_and_uba_stations_carry_point_geometry_identifiers_and_hourly_observations():
    records, _ = _parse("openaq")
    station = next(r for r in records if r["record_type"] == "station")
    assert station["geometry"]["coordinates"] == [13.41879, 52.51384]
    assert station["identifiers"] == {"eea_station_code": "DEBE068", "openaq_location_id": "2993"}
    series = [r for r in records if r["record_type"] == "observation_series"]
    assert {s["indicator"]["code"] for s in series} == {"no2", "pm10"}
    no2 = next(s for s in series if s["indicator"]["code"] == "no2")
    assert no2["kind"] == "observation" and no2["interval"] == "PT1H" and no2["unit"] == "µg/m³"
    assert [v["value"] for v in no2["values"]] == ["31.4", "28.9", "44.7", "52.1"]  # exact text, no float rounding
    uba, carry = _parse("uba")
    stations = [r for r in uba if r["record_type"] == "station"]
    assert {s["identifiers"]["eea_station_code"] for s in stations} == {"DEBE068", "DEBE065"}
    measures = next(r for r in uba if r["record_type"] == "observation_series" and r["native_id"] == "282:5:2")
    assert measures["values"][0]["start"] == "2026-09-24T00:00:00Z"  # 01:00 CET start -> UTC
    assert {v["status"] for v in measures["values"]} == {"provisional"}
    assert "declared by the source selection" in measures["status_basis"]
    frankfurter = next(r for r in uba if r["native_id"] == "271:5:2")
    assert frankfurter["values"][-1]["value"] is None


def test_entsoe_records_keep_zone_resolution_kind_and_published_unavailability():
    records, carry = _parse("entsoe")
    generation = [r for r in records if r["event_type"] == "generation"]
    assert {g["production_type"]["label"] for g in generation} == {"Fossil Brown coal/Lignite", "Wind Onshore"}
    assert all(g["kind"] == "observation" and g["resolution"] == "PT15M" and g["unit"] == "MW" for g in generation)
    load = {r["kind"]: r for r in records if r["event_type"] == "load"}
    assert set(load) == {"observation", "forecast"}
    assert load["forecast"]["document"]["issue_time"] == "2026-09-23T09:45:02Z"
    outages = {r["unavailability"]["kind"]: r for r in records if r["event_type"] == "unavailability"}
    assert set(outages) == {"planned", "unplanned"}
    unplanned = outages["unplanned"]["unavailability"]
    assert unplanned["reason"] == [{"code": "B18", "text": "Failure: gas turbine trip, cause under investigation (as published)"}]
    assert unplanned["nominal_capacity"] == "300" and unplanned["start"] == "2026-09-24T01:40:00Z"
    assert outages["unplanned"]["points"][0]["end"] == "2026-09-24T18:00:00Z"  # A03 block holds to interval end
    assert all(r["bidding_zone"]["code"] == "10Y1001A1001A82H" for r in records)


def test_entsoe_no_data_acknowledgement_is_coverage_not_an_outage():
    step = {"parse": "entsoe", "url": "https://web-api.tp.entsoe.eu/api", "params": {},
            "context": {"document": "unavailability", "zone": "10YLU-CEGEDEL-NQ"}}
    raw = (fixture_builder.RAW / "entsoe_ack_no_data.xml").read_bytes()
    records, carry = ep.parse_step(step, raw, {})
    assert records == [] and carry["entsoe_no_data"] == ["unavailability"]
    rejected = raw.replace(b"<code>999</code>", b"<code>401</code>")
    with pytest.raises(ProviderError) as error:
        ep.parse_step(step, rejected, {})
    assert error.value.code == "provider_rejected"


def test_smard_eea_and_ets_parse_as_published_without_determinations():
    smard, _ = _parse("smard")
    forecast = next(r for r in smard if r["kind"] == "forecast")
    assert forecast["document"]["issue_time"] == "2026-09-23T00:00:00Z"
    actual = next(r for r in smard if r["kind"] == "observation")
    assert actual["points"][-1]["value"] is None and actual["unit"] == "MWh"
    eea, _ = _parse("eea-industry")
    mitte = next(r for r in eea if "Mitte" in r["title"])
    assert mitte["identifiers"]["ets_identifier"] == "DE_900201" and mitte["permits"][0]["url"].startswith("https://")
    assert {(r["pollutant"], r["method"], r["value"]) for r in mitte["releases"]} == {
        ("CO2", "C", "612000000"), ("NOX", "M", "187500.5")}
    ets, carry = _parse("eu-ets")
    assert carry["ets_missing_installations"] == []
    verified = next(r for r in ets if r["native_id"] == "DE_900201:verified_emissions")
    compliance = verified["values"][0]["flags"]["compliance"]
    assert compliance["code"] == "A" and compliance["text"] is None and "no compliance determination" in compliance["note"]
    assert all(r["record_type"] != "facility" or r["geometry"] is None for r in ets)
    assert not any("DE_900203" in r["native_id"] for r in ets)  # unselected rows are not ingested


def test_open_meteo_is_model_or_forecast_and_dwd_recent_is_provisional_observation():
    archive, _ = _parse("open-meteo-archive")
    assert {r["kind"] for r in archive} == {"model"}
    assert archive[0]["model"]["name"] == "ERA5" and archive[0]["model"]["grid_resolution_m"] == 25000
    forecast, _ = _parse("open-meteo-forecast")
    assert {r["kind"] for r in forecast} == {"forecast"}
    assert forecast[0]["model"]["issue_time"] == "2026-09-26T03:00:00Z"
    dwd, _ = _parse("dwd")
    temperature = next(r for r in dwd if r.get("indicator", {}).get("code") == "TT_TU")
    assert temperature["kind"] == "observation" and {v["status"] for v in temperature["values"]} == {"provisional"}
    assert temperature["values"][3]["value"] is None  # -999 is a published missing value
    station = next(r for r in dwd if r["record_type"] == "station")
    assert station["title"] == "Berlin-Alexanderplatz" and station["geometry"]["coordinates"] == [13.4057, 52.5198]


@pytest.mark.parametrize("provider,raw,broken", [
    ("openaq", "openaq_location_2993.json", b'{"results": []}'),
    ("uba", "uba_stations.json", b'{"data": {}}'),
    ("smard", "smard_410_DE_quarterhour_1789941600000.json", b'{"series": "x"}'),
    ("eea-industry", "eea_industry_berlin_2024.json", b'{"results": [{"facilityName": "x"}]}'),
    ("eu-ets", "eu_ets_verified_2025.csv", b"REGISTRY_CODE;OTHER\nDE;1\n"),
    ("open-meteo-archive", "open_meteo_archive_era5.json", b"not json"),
])
def test_parsers_fail_closed_on_drift(provider, raw, broken):
    with pytest.raises(ProviderError) as error:
        _parse(provider, overrides={raw: broken})
    assert error.value.code == "schema_drift"


def test_durable_http_client_needs_credentials_and_never_stores_them():
    env = harness.Env()
    client = ep.EnvironmentClient(env.client("entsoe").http, principal_id="alice", secret=None)
    with pytest.raises(ProviderError) as error:
        client.acquire(harness.selections()["entsoe"], "no-token")
    assert error.value.code == "authentication_failed"
    result = env.acquire("entsoe")
    assert result["ok"] and result["execution"] == "injected"
    receipts = [row[0] for row in env.conn.execute("SELECT receipt_json FROM provider_execution_requests").fetchall()]
    assert receipts and not any(ep.FIXTURE_SECRET in r for r in receipts)
    assert all(json.loads(r)["request"]["credential_slots"]["params"] == ["securityToken"] for r in receipts)
