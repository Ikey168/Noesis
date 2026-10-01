"""PEGELONLINE, USGS Water Data and EEA WISE acquisition through the source-pack runtime (#2582, WA01, WA03-WA05)."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator

from src.ingestion import water_sources as ws
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit.water import fixture_builder
from tests.unit.water import harness as h

NS = h.NS


def _source(source_id):
    return next(s for s in h.manifest()["sources"] if s["source_id"] == source_id)


def test_source_pack_is_pinned_and_validates_against_the_schema():
    manifest = h.manifest()
    schema = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-source-pack-v1.json").read_text())
    assert not list(Draft7Validator(schema).iter_errors(manifest))
    assert {s["water"]["provider"]: s["water"]["live_verification"] for s in manifest["sources"]} == {
        "pegelonline": "unverified-live", "usgs": "unverified-live", "eea-wise": "unverified-live"}
    assert {p: v["status"] for p, v in ws.LIVE_VERIFICATION.items()} == {
        "pegelonline": "unverified-live", "usgs": "unverified-live", "eea-wise": "unverified-live",
        "grdc": "not-implemented"}
    assert "redistribution" in ws.NOT_IMPLEMENTED["grdc"]["reason"]
    usgs = _source("usgs-water-data")
    assert usgs["auth"] == {"kind": "optional-secret", "secret_ref": "NOESIS_USGS_WATER_API_KEY"}
    built = fixture_builder.build(write=False)
    for path, text in built.items():
        assert path.read_text() == text, path
    report = SourcePackConformance(h.ROOT).offline(json.loads(h.PACK.read_text()))
    assert report["valid"] and report["coverage"] == {"configured": 4, "verified": 4}


def test_pegelonline_stations_keep_uuid_number_gauge_zero_validity_and_raw_values_without_resampling():
    records = [r["water_record"] for r in ws.replay_native_fixture(
        _source("pegelonline-stations-levels"), json.loads((h.ROOT / "tests/fixtures/source_packs/"
                                                            "water-pegelonline.json").read_text()))]
    stations = {r["record_key"]: r for r in records if r["record_type"] == "station"}
    dresden = stations[fixture_builder.DRESDEN]["as_published"]
    assert dresden["number"] == "990001" and dresden["river"] == {"shortname": "ELBE", "longname": "ELBE"}
    assert dresden["datums"] == [{"kind": "gauge-zero", "series": "W", "value": "102.73", "unit": "m. ü. NHN",
                                  "reference": "m. ü. NHN", "valid_from": "2019-11-01"}]
    assert {t["shortname"] for t in dresden["thresholds"]} == {"MNW", "MHW"}
    assert {"scheme": "pegelonline-number", "value": "990001"} in dresden["identifiers"]
    levels = [r["as_published"] for r in records if r["record_type"] == "observation"
              and r["as_published"]["parameter"] == "W" and fixture_builder.DRESDEN in r["record_key"]]
    assert [o["time"] for o in levels] == [t for t, _ in fixture_builder.DRESDEN_W]  # as published, 00:45 absent
    assert {o["unit"] for o in levels} == {"cm"} and levels[0]["value"] == "212.0"
    assert {o["quality"]["state"] for o in levels} == {"provisional"}
    assert "unchecked" in levels[0]["quality"]["published"]


def test_usgs_values_keep_approval_status_and_qualifiers_per_value():
    records = [r["water_record"] for r in ws.replay_native_fixture(
        _source("usgs-water-data"), json.loads((h.ROOT / "tests/fixtures/source_packs/water-usgs.json").read_text()))]
    location = next(r for r in records if r["record_type"] == "station")["as_published"]
    assert location["datums"][0]["kind"] == "vertical-datum" and location["datums"][0]["reference"] == "NAVD88"
    assert {"scheme": "us-county-fips", "value": "24031"} in location["identifiers"]
    values = [r["as_published"] for r in records if r["record_type"] == "observation"]
    assert {(v["parameter"], v["quality"]["state"]) for v in values} == {
        ("00060", "provisional"), ("00065", "provisional"), ("00060", "approved")}
    estimated = next(v for v in values if v["time"] == "2026-09-20T12:30:00+00:00")
    assert estimated["qualifiers"] == ["Estimated"] and estimated["value"] == "4180"
    daily = next(v for v in values if v["statistic"] == "00003")
    assert daily["time"] == "2026-09-18" and daily["quality"]["published"] == "Approved"


def test_wise_status_is_keyed_by_code_and_cycle_and_cycles_stay_separate():
    records = [r["water_record"] for r in ws.replay_native_fixture(
        _source("eea-wise-wfd-status"), json.loads((h.ROOT / "tests/fixtures/source_packs/"
                                                    "water-eea-wise-status.json").read_text()))]
    assert sorted(r["record_key"] for r in records) == [
        "DERW_DESN_FIX-0001|2016", "DERW_DESN_FIX-0001|2022", "DERW_DESN_FIX-0002|2016"]
    elbe = next(r for r in records if r["record_key"] == "DERW_DESN_FIX-0001|2022")["as_published"]
    assert elbe["status_elements"] == [
        {"element": "ecological_status_or_potential", "value": "Poor",
         "published_field": "swEcologicalStatusOrPotentialValue", "assessment_year": "2020"},
        {"element": "chemical_status", "value": "Failing to achieve good", "published_field": "swChemicalStatusValue",
         "assessment_year": "2021"}]
    assert {r["subject"]["key"] for r in records} == {"eu-wb:DERW_DESN_FIX-0001", "eu-wb:DERW_DESN_FIX-0002"}


def test_runtime_run_has_bounded_windows_receipts_and_licence_attribution():
    env = h.Env().loaded()
    runs = {r["source_id"]: r for r in env.store.runs(NS)}
    assert set(runs) == set(h.SOURCES) and {r["status"] for r in runs.values()} == {"complete"}
    windows = [o for o in runs["pegelonline-stations-levels"]["outcomes"] if o["window"]]
    assert len(windows) == 3 and all(o["response_sha256"] and o["url"].startswith("https://") for o in windows)
    assert windows[0]["window"] == fixture_builder.WINDOW
    revision = env.store.current(NS, env.store.find(NS, "station", "pegelonline", fixture_builder.DRESDEN)[
        "record_id"])
    assert "DL-DE" in revision["statement"]["source"]["licence"] and revision["evidence_origin"] == "fixture"
    before = env.store.generation(NS)
    env.advance(1)
    assert env.run("water-replay")["status"] == "complete"
    assert env.store.generation(NS) == before  # a replay adds nothing


def test_later_run_revises_withdraws_and_adds_a_cycle():
    env = h.Env().loaded()
    env.advance(8)
    assert env.run("water-later", later=True)["status"] == "complete"
    meissen = env.store.find(NS, "station", "pegelonline", fixture_builder.MEISSEN)
    assert [r["event"] for r in env.store.revisions(NS, meissen["record_id"])] == ["published", "removed"]
    cycle = env.store.find(NS, "water_body_assessment", "eea-wise", "DERW_DESN_FIX-0002|2022")
    assert cycle and env.store.revisions(NS, cycle["record_id"])[0]["changes"] == ["created"]


@pytest.mark.parametrize("change", [
    {"kind": "measurements", "uuid": fixture_builder.DRESDEN, "series": "W", "start": "2026-08-01T00:00:00+02:00",
     "end": "2026-09-20T00:00:00+02:00"},
    {"kind": "measurements", "uuid": "not-a-uuid", "series": "W", **fixture_builder.WINDOW},
    {"kind": "measurements", "uuid": fixture_builder.DRESDEN, "series": "W", "start": "2026-09-20T00:00:00",
     "end": "2026-09-21T00:00:00"},
    {"kind": "forecast", "uuid": fixture_builder.DRESDEN},
])
def test_unbounded_or_undeclared_pegelonline_selections_are_refused(change):
    source = copy.deepcopy(_source("pegelonline-stations-levels"))
    source["water"]["selection"] = [change]
    with pytest.raises(SourcePackError):
        ws.selection_entries(source)


def test_usgs_and_wise_selections_are_bounded_and_hosts_pinned():
    usgs = copy.deepcopy(_source("usgs-water-data"))
    usgs["water"]["selection"] = [{**fixture_builder.USGS_SELECTION[1], "limit": 5000}]
    with pytest.raises(SourcePackError):
        ws.selection_entries(usgs)
    wise = copy.deepcopy(_source("eea-wise-wfd-status"))
    wise["water"]["selection"] = [{**fixture_builder.STATUS_SELECTION, "table": "users; DROP TABLE x"}]
    with pytest.raises(SourcePackError):
        ws.selection_entries(wise)
    moved = copy.deepcopy(_source("usgs-water-data"))
    moved["endpoint"] = "https://example.org"
    with pytest.raises(SourcePackError):
        ws.selection_entries(moved)


def test_personal_fields_in_a_response_are_dropped_and_named_in_the_receipt():
    source = _source("usgs-water-data")
    pages = fixture_builder.usgs_pages()
    pages[0] = copy.deepcopy(pages[0])
    pages[0]["body"]["properties"]["contact_email"] = "someone@example.org"
    adapter = ws.WaterSourceAdapter(source, transport=ws.fixture_transport(pages), secret=ws.FIXTURE_SECRET)
    page = adapter.fetch_page({"operation": "water", "parameters": {}, "limit": 100}, cursor=None)
    assert page.receipt["personal_fields_dropped"] == ["properties.contact_email"]
    assert "someone@example.org" not in json.dumps([r["water_record"] for r in page.records])


def test_schema_drift_fails_closed():
    source = _source("eea-wise-wfd-status")
    pages = [{**fixture_builder.wise_status_pages()[0], "body": {"rows": []}}]
    adapter = ws.WaterSourceAdapter(source, transport=ws.fixture_transport(pages))
    with pytest.raises(SourcePackError) as error:
        adapter.fetch_page({"operation": "water", "parameters": {}, "limit": 100}, cursor=None)
    assert error.value.code == "schema_drift"
