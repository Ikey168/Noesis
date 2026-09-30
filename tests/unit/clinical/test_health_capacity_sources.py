"""Health-system capacity sources (#2215): the HS01 audit and WHO GHO / OECD / Eurostat acquisition (HS03-HS05)."""

from __future__ import annotations

import json
import re

import pytest

from src.ingestion import clinical_providers as cp
from src.ingestion import health_capacity_sources as hs
from src.ingestion import surveillance_sources as ss
from src.ingestion.source_packs import SourcePackConformance, SourcePackError, validate_source_pack
from tests.unit import health_capacity_fixture_builder as fb
from tests.unit.clinical import health_capacity_harness as h

# ---------------------------------------------------------------------- HS01 audit


def test_every_source_has_an_access_decision_confirming_reuse_of_the_existing_paths():
    assert set(hs.CAPACITY_CONTRACTS) == {"who-gho", "oecd-health", "eurostat-health"}
    for contract in hs.CAPACITY_CONTRACTS.values():
        for key in ("endpoints", "terms", "attribution", "rate_limits", "definitions", "revision_behaviour", "reuses"):
            assert contract[key], key
        assert contract["access_decision"] == "unverified-live"
    assert "who-gho-odata" in hs.CAPACITY_CONTRACTS["who-gho"]["reuses"]
    assert "eurostat-sdmx-csv" in hs.CAPACITY_CONTRACTS["eurostat-health"]["reuses"]
    assert "SDMX connector" in hs.CAPACITY_CONTRACTS["oecd-health"]["reuses"]
    # WHO GHO and Eurostat stay the surveillance providers; OECD is the one new provider in the clinical contracts.
    assert cp.PROVIDER_CONTRACTS["oecd-health"]["record_owner"] == "src.kb.health_capacity"
    assert cp.PROVIDER_CONTRACTS["who-gho"]["record_owner"] == "src.kb.surveillance"
    assert cp.LIVE_VERIFICATION["oecd-health"]["status"] == "unverified-live"
    assert cp.PROVIDER_HOSTS["oecd-health"] == {"sdmx.oecd.org"}


def test_the_bounded_coverage_names_indicator_codes_places_and_years_and_excludes_rankings():
    indicators = hs.BOUNDED_COVERAGE["indicators"]
    for provider in ("who-gho", "oecd-health", "eurostat-health"):
        assert set(indicators[provider]) == {"beds", "workforce", "expenditure"}
    assert "WHS6_102" in indicators["who-gho"]["beds"][0]
    assert "hlth_sha11_hf" in indicators["eurostat-health"]["expenditure"][0]
    assert hs.BOUNDED_COVERAGE["places"]["aggregates_kept_as_aggregates"]
    assert any("ranking" in item for item in hs.BOUNDED_COVERAGE["excluded"])
    audit = (h.ROOT / "docs/roadmaps/clinical-health-capacity-source-audit.md").read_text()
    for needle in ("who-gho-odata", "eurostat-sdmx-csv", "SDMX connector", "_verify_", "WHS6_102", "hlth_rs_bds1",
                   "hlth_sha11_hf", "OBS_STATUS", "Places and years", "never resolved to or treated as a country",
                   "No health-system performance ranking"):
        assert needle in audit, needle


def test_the_sources_are_entries_of_the_existing_pack_with_pinned_fixtures_that_replay():
    manifest = validate_source_pack(json.loads(h.PACK.read_text()))
    assert manifest["pack_id"] == "clinical-evidence" and manifest["version"] == "0.1.4"  # 0.1.4 devices (#2654)
    capacity = [s for s in manifest["sources"] if s["connector"] == "health-capacity"]
    assert {s["source_id"] for s in capacity} == set(h.SOURCES.values())
    assert {s["surveillance"]["format"] for s in capacity} == set(hs.FORMATS)
    assert all(s["mapping"]["target_schema"] == "noesis-surveillance-record-v1" for s in capacity)
    assert all(d["capacity_domain"] in hs.CAPACITY_DOMAINS for s in capacity for d in s["surveillance"]["documents"])
    result = SourcePackConformance(h.ROOT).offline(manifest)
    assert result["valid"], result["sources"]
    for source in capacity:
        fixture = json.loads((h.ROOT / source["fixture"]["path"]).read_text())
        assert fixture["authored"] is True and "Not live evidence" in fixture["note"]


def test_capacity_fixtures_and_manifest_are_pinned_and_in_sync():
    from tests.unit import medicines_fixture_builder as mfb
    from tests.unit import surveillance_fixture_builder as sfb

    manifest = json.loads(h.PACK.read_text())
    assert fb.build(write=False) == manifest
    assert sfb.build(write=False) == manifest and mfb.build(write=False) == manifest


def test_the_connector_accepts_only_capacity_documents_of_the_three_formats():
    source = next(s for s in validate_source_pack(json.loads(h.PACK.read_text()))["sources"]
                  if s["source_id"] == h.SOURCES["gho"])
    missing = json.loads(json.dumps(source))
    del missing["surveillance"]["documents"][0]["capacity_domain"]
    with pytest.raises(SourcePackError) as caught:
        hs.capacity_declaration(missing)
    assert caught.value.code == "invalid_manifest"
    other = json.loads(json.dumps(source))
    other["surveillance"]["format"] = "rki-github-csv"
    with pytest.raises(SourcePackError):
        hs.capacity_declaration(other)
    wrong = dict(fb.gho_document("WHS6_102"), capacity_domain="quality")
    with pytest.raises(ss.SurveillanceFormatError):
        ss.capacity_condition(wrong)


# ---------------------------------------------------------------------- HS03 WHO GHO


@pytest.fixture
def env():
    environment = h.Env()
    receipt = environment.acquire("r1")
    assert receipt["status"] == "complete"
    return environment


def test_gho_indicators_become_capacity_series_keyed_by_code_place_year_and_publish_state(env):
    beds = env.indicator("who-gho", "WHS6_102", "DEU")
    assert beds["domain"] == "beds" and beds["unit"]["label"] == "per 10 000 population"
    assert beds["dimensions"] == {"PublishState": "PUBLISHED"}
    assert beds["indicator"]["label"] == "Hospital beds (per 10 000 population)"
    assert {"kind": "gho-indicator", "identifier": "WHS6_102"} in beds["citations"]
    region = env.indicator("who-gho", "WHS6_102", "EUR")
    assert region["place"]["system"] == "who-region" and region["aggregate"] is True
    # Indicator metadata (definition, unit, method) is a definition record with the retrieval time.
    definition = beds["definition"]
    assert "Method of estimation" in definition["text"] and definition["locator"].endswith("/WHS6_102")
    assert definition["retrieved_at_ms"] and definition["declared_on"] == "2098-03-10"
    values = env.capacity().observations(h.NS, beds["series_id"], scopes=h.READ_ONLY)["values"]
    assert [(v["reference_period"], v["value"]) for v in values] == [("2096", "80.1"), ("2097", "79.4")]
    expenditure = env.indicator("who-gho", "GHED_CHEGDP_SHA2011", "DEU")
    missing = env.capacity().observations(h.NS, expenditure["series_id"], scopes=h.READ_ONLY)["values"][-1]
    assert missing["value"] is None and missing["status"] == "unknown"
    assert "missing-as-published" in missing["flags"]


def test_gho_reacquisition_adds_nothing_unless_values_change_then_it_is_a_new_vintage(env):
    beds = env.indicator("who-gho", "WHS6_102", "DEU")
    assert env.acquire("r2", keys=["gho"])["status"] == "complete"
    assert env.indicator("who-gho", "WHS6_102", "DEU")["vintage_count"] == 1
    env.gho_revision("WHS6_102")
    env.acquire("r3", keys=["gho"])
    revised = env.indicator("who-gho", "WHS6_102", "DEU")
    assert revised["vintage_count"] == 2
    store = env.capacity()
    latest = store.observations(h.NS, beds["series_id"], scopes=h.READ_ONLY)
    first = store.observations(h.NS, beds["series_id"], scopes=h.READ_ONLY, as_of="2098-06-01")
    assert latest["values"][-1]["value"] == "79.9" and first["values"][-1]["value"] == "79.4"
    assert first["later_vintages"] == 1


# ---------------------------------------------------------------------- HS04 OECD


def test_oecd_dataflows_keep_status_flags_and_notes_verbatim_through_the_sdmx_connector(env):
    france = env.indicator("oecd-health", "DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC:HOSP_BEDS", "FRA")
    assert france["indicator"]["version"] == "1.0"
    assert france["indicator"]["dataflow"] == fb.oecd_dataflow("beds")
    assert france["indicator"]["country_note"] == fb.OECD_FLOWS["beds"]["country_notes"]["FRA"]
    assert france["indicator"]["source_note"] == fb.OECD_FLOWS["beds"]["source_note"]
    assert [b["capacity_kind"] for b in france["breaks"]] == ["publisher-flagged-break"]
    values = env.capacity().observations(h.NS, france["series_id"], scopes=h.READ_ONLY)["values"]
    assert values[-1]["flags"] == ["b: break in time series (OBS_STATUS B)"]
    assert values[-1]["attributes"] == {"OBS_STATUS": "B", "UNIT_MULT": "0"}
    germany = env.indicator("oecd-health", "DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC:HOSP_BEDS", "DEU")
    assert "country_note" in germany["indicator"]
    total = env.indicator("oecd-health", "DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC:HOSP_BEDS", "OECD")
    assert total["place"]["system"] == "oecd-aggregate" and total["aggregate"] is True
    revision = env.capacity().observations(h.NS, total["series_id"], scopes=h.READ_ONLY)["source_revision"]
    assert revision["native_revision"] == fb.oecd_dataflow("beds") and revision["release_basis"] == "http_last_modified"


def test_an_oecd_dataflow_version_change_is_a_new_vintage(env):
    germany = env.indicator("oecd-health", "DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC:HOSP_BEDS", "DEU")
    env.oecd_version("1.1")
    assert env.acquire("r2", keys=["oecd"])["status"] == "complete"
    after = env.indicator("oecd-health", "DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC:HOSP_BEDS", "DEU")
    assert after["series_id"] == germany["series_id"] and after["vintage_count"] == 2
    assert after["indicator"]["version"] == "1.1"
    old = env.capacity().observations(h.NS, after["series_id"], scopes=h.READ_ONLY, as_of="2098-08-01")
    new = env.capacity().observations(h.NS, after["series_id"], scopes=h.READ_ONLY)
    assert old["source_revision"]["native_revision"].endswith(",1.0") and old["values"][-1]["value"] == "7.8"
    assert new["source_revision"]["native_revision"].endswith(",1.1") and new["values"][-1]["value"] == "7.7"


def test_an_oecd_answer_for_another_version_or_an_unknown_status_is_refused():
    document = fb.oecd_document("beds")
    with pytest.raises(ss.SurveillanceFormatError) as caught:
        ss.parse_oecd(fb.oecd_csv("beds", "1.1").encode(), document=document)
    assert "another dataflow or version" in str(caught.value)
    bad = fb.oecd_csv("beds").replace(",P,0", ",Z,0")
    with pytest.raises(ss.SurveillanceFormatError, match="OBS_STATUS"):
        ss.parse_oecd(bad.encode(), document=document)
    with pytest.raises(ss.SurveillanceFormatError, match="capacity domain"):
        ss.parse_oecd(fb.oecd_csv("beds").encode(), document={k: v for k, v in document.items()
                                                                if k != "capacity_domain"})
    assert re.search(r"format=csvfile", ss.document_url(document, fb.OECD_ENDPOINT, "oecd-sdmx-csv"))


# ---------------------------------------------------------------------- HS05 Eurostat


def test_eurostat_resources_and_expenditure_keep_flags_verbatim_and_updates_are_vintages(env):
    germany = env.indicator("eurostat-health", "hlth_rs_bds1:HBEDT", "DE")
    assert germany["domain"] == "beds" and germany["unit"]["label"] == "per 100 000 population"
    assert germany["indicator"]["measure_dimension"] == "facility"
    assert {"kind": "eurostat-dataset", "identifier": "hlth_rs_bds1"} in germany["citations"]
    eu = env.indicator("eurostat-health", "hlth_rs_bds1:HBEDT", "EU27_2020")
    assert eu["place"]["system"] == "eurostat-aggregate" and eu["aggregate"] is True
    values = env.capacity().observations(h.NS, germany["series_id"], scopes=h.READ_ONLY)["values"]
    assert values[-1]["flags"] == ["p: provisional"]
    hf1 = env.indicator("eurostat-health", "hlth_sha11_hf:HF1", "DE")
    assert hf1["domain"] == "expenditure" and hf1["unit"]["label"] == "percent of current health expenditure"
    assert {b["capacity_kind"] for b in hf1["breaks"]} == {"definition-break", "publisher-flagged-break"}
    env.eurostat_update("hlth_rs_bds1")
    env.acquire("r2", keys=["eurostat"])
    updated = env.indicator("eurostat-health", "hlth_rs_bds1:HBEDT", "DE")
    assert updated["vintage_count"] == 2
    latest = env.capacity().observations(h.NS, germany["series_id"], scopes=h.READ_ONLY)
    assert latest["values"][-1]["value"] == "783.4" and latest["values"][-1]["flags"] == []
    assert latest["source_revision"]["release_basis"] == "eurostat_last_update"
    # The unchanged expenditure dataset adds no vintage.
    assert env.indicator("eurostat-health", "hlth_sha11_hf:HF1", "DE")["vintage_count"] == 1


def test_acquisition_is_receipted_as_fixture_evidence(env):
    rows = env.conn.execute(
        "SELECT DISTINCT r.evidence_origin FROM surveillance_releases r WHERE r.source_id IN (?, ?, ?)",
        sorted(h.SOURCES.values())).fetchall()
    assert rows == [("fixture",)]
