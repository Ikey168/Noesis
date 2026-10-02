"""WA01 and WA03-WA05 (#2587, #2597, #2602, #2607): contracts, bounded acquisition, receipts and revisions."""

import json

import pytest

from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    validate_source_pack,
)
from src.ingestion.water_sources import (
    BOUNDED_COVERAGE,
    LIVE_VERIFICATION,
    NOT_IMPLEMENTED,
    PROVIDER_CONTRACTS,
    WaterSourceAdapter,
    fixture_transport,
    parse_wise_status,
    personal_hints,
    selection_entries,
)
from src.kb.water_records import MINIMISATION
from tests.unit.water import fixture_builder, harness
from tests.unit.water.harness import NS

EXAMPLA = fixture_builder.EXAMPLA


def _source(provider):
    return json.loads(json.dumps(next(s for s in harness.manifest()["sources"] if s["water"]["provider"] == provider)))


def _current(env, record_type, provider, key):
    return env.store.current(NS, env.store.find(NS, record_type, provider, key)["record_id"])


def test_fixtures_are_pinned_and_in_sync():
    for path, text in fixture_builder.build(write=False).items():
        assert path.read_text(encoding="utf-8") == text, path
    result = SourcePackConformance(harness.ROOT).offline(json.loads(harness.PACK.read_text()))
    assert result["valid"] and len(result["sources"]) == 3


def test_audit_records_contracts_minimisation_coverage_and_live_state():
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert {"licence", "terms_url", "attribution", "redistribution", "rate_limits", "authentication",
                "versioning", "access", "endpoints", "updates_corrections_removals"} <= set(contract), provider
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    assert NOT_IMPLEMENTED["grdc"]["status"] == "not-implemented" and LIVE_VERIFICATION["grdc"]["status"] == \
        "not-implemented"
    assert "NOESIS_USGS_WATER_API_KEY" in PROVIDER_CONTRACTS["usgs"]["authentication"]
    assert set(BOUNDED_COVERAGE) >= {"pegelonline", "usgs", "eea-wise", "caps", "justification"}
    pack = harness.manifest()
    usgs = next(s for s in pack["sources"] if s["water"]["provider"] == "usgs")
    assert usgs["auth"] == {"kind": "optional-secret", "secret_ref": "NOESIS_USGS_WATER_API_KEY"}
    assert {s["water"]["live_verification"] for s in pack["sources"]} == {"unverified-live"}
    audit = (harness.ROOT / "docs/development/water-evidence/source-audit.md").read_text()
    for heading in ("Data minimisation", "Bounded coverage", "GRDC", "Live verification", "not re-verified live"):
        assert heading in audit, heading
    assert MINIMISATION["who_may_query"]


def test_selections_are_bounded_and_fail_closed():
    po = _source("pegelonline")
    too_long = [{**fixture_builder.PO_SELECTION[0], "end": "2026-12-01T00:00:00+01:00"}]
    unknown_series = [{**fixture_builder.PO_SELECTION[0], "timeseries": ["LT"]}]
    for bad in (too_long, unknown_series, [{"kind": "station", "uuid": "x"}]):
        with pytest.raises(SourcePackError):
            selection_entries({**po, "water": {**po["water"], "selection": bad}})
    usgs = _source("usgs")
    for bad in ([{"kind": "daily", "monitoring_location_id": "USGS-99990001", "parameter_code": "00060",
                  "statistic_id": "00003", "time": "2026-01-01/2026-06-01", "limit": 10}],
                [{"kind": "daily", "monitoring_location_id": "USGS-99990001", "parameter_code": "00060",
                  "statistic_id": "00003", "time": "2026-09-01/2026-09-02", "limit": 5000}]):
        with pytest.raises(SourcePackError):
            selection_entries({**usgs, "water": {**usgs["water"], "selection": bad}})
    wise = _source("eea-wise")
    with pytest.raises(SourcePackError):  # codes are validated before they reach the SQL template
        selection_entries({**wise, "water": {**wise["water"], "selection": [
            {"kind": "water_body_status", "eu_codes": ["DE'; DROP TABLE x;--"], "cycles": ["2022"]}]}})
    with pytest.raises(SourcePackError):
        selection_entries({**po, "endpoint": "https://example.org"})


def test_pegelonline_stations_keyed_by_uuid_and_number_with_gauge_zero_and_raw_values_as_published():
    env = harness.Env().loaded()
    station = _current(env, "station", "pegelonline", EXAMPLA)["statement"]["as_published"]
    assert station["number"] == "59990001" and station["river"] == {"name": "NORDFLUSS", "identifier": "NORDFLUSS",
                                                                   "scheme": "pegelonline-water"}
    assert station["datum"]["gauge_zero"] == {"value": 30.12, "unit": "m. ü. NHN", "valid_from": "2019-11-01"}
    assert {t["name"]: t["value"] for t in station["thresholds"]} == {"MW": 320, "MHW": 520}
    levels = [env.store.current(NS, r["record_id"])["statement"]["as_published"]
              for r in env.store.records(NS, record_type="observation", provider="pegelonline")
              if r["record_key"].startswith(f"{EXAMPLA}|W|")]
    assert [o["time"] for o in levels][:3] == ["2026-09-20T00:00:00+02:00", "2026-09-20T00:15:00+02:00",
                                              "2026-09-20T00:30:00+02:00"]
    assert "2026-09-20T00:45:00+02:00" not in {o["time"] for o in levels}  # the gap stays missing
    assert {o["unit"] for o in levels} == {"cm"} and {o["quality"]["state"] for o in levels} == {"provisional"}
    run = [r for r in env.store.runs(NS) if r["source_id"] == "pegelonline-stations-levels"][-1]
    assert run["evidence_origin"] == "fixture"
    assert all(o["window"] == {"start": fixture_builder.START, "end": fixture_builder.END} for o in run["outcomes"])
    assert all("WSV" in o["attribution"] for o in run["outcomes"])


def test_usgs_provisional_and_approved_values_are_separate_revisions_with_qualifiers():
    env = harness.Env().loaded()
    key = "USGS-99990001|00060|00003|2026-09-04"
    first = _current(env, "observation", "usgs", key)["statement"]["as_published"]
    assert first["quality"]["state"] == "provisional" and first["qualifiers"] == ["e"] and first["value"] == 14.1
    env.advance(7)
    assert env.run("water-later", later=True)["status"] == "complete"
    record = env.store.find(NS, "observation", "usgs", key)
    chain = env.store.revisions(NS, record["record_id"])
    assert [(r["statement"]["as_published"]["quality"]["state"], r["statement"]["as_published"]["value"])
            for r in chain] == [("provisional", 14.1), ("approved", 14.0)]
    withdrawn = env.store.revisions(NS, env.store.find(NS, "observation", "usgs",
                                                       "USGS-99990001|00060|00003|2026-09-06")["record_id"])
    assert [r["event"] for r in withdrawn] == ["published", "removed"]
    assert env.store.find(NS, "observation", "usgs", "USGS-99990001|00060|00003|2026-09-05") is None
    usgs = [r for r in env.store.runs(NS) if r["source_id"] == "usgs-water-data"][-1]
    assert any(o["removed"] == 1 for o in usgs["outcomes"])
    location = _current(env, "station", "usgs", "USGS-99990001")["statement"]["as_published"]
    assert location["hydrologic_unit_code"] == "020700089999" and location["datum"]["vertical_datum"] == "NAVD88"


def test_wise_status_is_keyed_by_code_and_cycle_and_cycles_are_never_merged():
    env = harness.Env().loaded()
    cycles = {r["record_key"]: env.store.current(NS, r["record_id"])["statement"]["as_published"]
              for r in env.store.records(NS, record_type="assessment")}
    assert set(cycles) == {"DEFX_EXAMPLA_01|2016", "DEFX_EXAMPLA_01|2022", "DEFX_EXAMPLA_02|2016",
                           "DEFX_EXAMPLA_02|2022"}
    assert cycles["DEFX_EXAMPLA_01|2016"]["ecological"]["label"] == "Moderate"
    assert cycles["DEFX_EXAMPLA_01|2022"]["ecological"]["label"] == "Poor"
    assert cycles["DEFX_EXAMPLA_02|2022"]["ecological"]["value"] == "Unknown"
    assert cycles["DEFX_EXAMPLA_01|2022"]["chemical"]["label"] == "Failing to achieve good"
    body = _current(env, "water_body", "eea-wise", "DEFX_EXAMPLA_01")["statement"]["as_published"]
    assert body["geometry"]["source"]["cycle"] == "2022" and body["category"] == "river"
    lake = _current(env, "water_body", "eea-wise", "DEFX_EXAMPLA_02")["statement"]
    assert lake["as_published"]["geometry"] is None and any("geometry" in u for u in lake["unknowns"])
    run = [r for r in env.store.runs(NS) if r["source_id"] == "eea-wise-wfd-status"][-1]
    assert run["outcomes"][-1]["selection"]["cycles"] == ["2016", "2022"]
    env.advance(30)
    assert env.run("water-later", later=True)["status"] == "complete"
    new = env.store.find(NS, "assessment", "eea-wise", "DEFX_NORTHWIND_03|2022")
    published = env.store.current(NS, new["record_id"])["statement"]["as_published"]
    assert published["ecological"]["kind"] == "potential"  # heavily modified: potential, as reported


def test_personal_fields_in_a_payload_are_dropped_and_reported_never_stored():
    rows = [fixture_builder.wise_row("DEFX_X", "X", "RW", 2022, "2", "2")]
    rows[0]["competentAuthorityContactEmail"] = "someone@example.org"
    assert personal_hints({"results": rows}) == ["results.competentAuthorityContactEmail"]
    records = parse_wise_status({"results": rows}, "https://discodata.eea.europa.eu/sql", origin="fixture",
                                geometries={})
    assert "someone@example.org" not in json.dumps(records)


def test_failures_are_explicit_and_the_optional_key_is_reported():
    source = next(s for s in harness.Env().runtime._manifest("climate-environment-water")[0]["sources"]
                  if s["source_id"] == "usgs-water-data")
    limited = WaterSourceAdapter(source, transport=lambda **_: {"status": 429, "headers": {"Retry-After": "5"},
                                                                "content": b"", "origin": "fixture"})
    with pytest.raises(SourcePackError) as rate:
        limited.fetch_page({"operation": "water", "parameters": {}}, cursor=None)
    assert rate.value.code == "rate_limited"
    down = WaterSourceAdapter(source, transport=lambda **_: {"status": 503, "headers": {}, "content": b"",
                                                             "origin": "fixture"})
    with pytest.raises(SourcePackError) as error:
        down.fetch_page({"operation": "water", "parameters": {}}, cursor=None)
    assert error.value.code == "source_unavailable"
    pages = json.loads((harness.ROOT / source["fixture"]["path"]).read_text())["native_pages"]
    page = WaterSourceAdapter(source, transport=fixture_transport(pages)).fetch_page(
        {"operation": "water", "parameters": {}}, cursor=None)
    assert page.receipt["credential"].startswith("not configured")
    assert validate_source_pack(json.loads(harness.PACK.read_text()))["pack_id"] == "climate-environment-water"
