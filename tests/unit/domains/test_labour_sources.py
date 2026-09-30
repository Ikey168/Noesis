"""Labour source audit and acquisition: ILOSTAT, OECD, Eurostat LFS (SDMX) and BLS (#2448, #2457, #2463, #2464, #2468)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.labour_sources import (
    BOUNDED_COVERAGE,
    FIXTURE_SECRET,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    LabourStatisticsAdapter,
    decode_bls_series_id,
    fixture_transport,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import labour_harness as h


def test_every_provider_has_an_audited_contract_and_a_live_verification_entry():
    assert set(PROVIDER_CONTRACTS) == {"ilostat", "oecd", "eurostat-lfs", "bls"}
    for provider, contract in PROVIDER_CONTRACTS.items():
        for key in ("access", "authentication", "rate_limits", "terms", "indicators", "definition_basis",
                    "seasonal_adjustment", "release_signals", "revision_model"):
            assert contract[key], (provider, key)
        assert set(contract["indicators"]) == {"employment", "unemployment", "wages", "hours", "vacancies"}
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert "verified-live" in LIVE_VERIFICATION[provider]["intended"]
    assert "registration key" in PROVIDER_CONTRACTS["bls"]["authentication"]
    assert {"Germany", "United States", "California", "Berlin (NUTS 2 DE30)"} == set(BOUNDED_COVERAGE["places"])
    audit = (h.ROOT / "docs/development/labour-evidence/source-audit.md").read_text()
    for name in ("ILOSTAT", "OECD", "Eurostat", "BLS", "LIVE_VERIFICATION", "nowcast"):
        assert name in audit


def test_the_pinned_fixtures_replay_offline_through_the_real_adapter():
    manifest = h.manifest()
    assert manifest["version"] == "1.7.0"  # 1.7.0 adds the extractives sources (#2653); ours unchanged
    labour = [s for s in manifest["sources"] if s["connector"] == "labour-statistics"]
    assert {s["source_id"] for s in labour} == set(h.SOURCES.values())
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": labour})
    assert replay["valid"] and replay["coverage"]["verified"] == 4


def test_ilostat_keeps_modelled_estimates_and_national_series_apart_with_source_notes():
    records = [r for page in h.fetch("ilostat") for r in page]
    items = [r["labour_item"] for r in records]
    une = [i for i in items if i["indicator"]["concept"] == "unemployment_rate" and i["area"]["code"] == "DEU"]
    assert {i["estimate_type"] for i in une} == {"national-reported", "ilo-modelled"}
    national = next(i for i in une if i["estimate_type"] == "national-reported")
    assert national["definition"]["basis"] == "national" and national["dataflow"]["reference"].startswith(
        "ILO,DF_UNE_DEAP")
    notes = {(n["attribute"], n["value"]) for n in national["source_notes"]}
    assert ("SOURCE", "BA:1234") in notes and ("NOTE_SOURCE", "R1:3513") in notes
    assert any(n["kind"] == "break" and n["periods"] == ["2098"] for n in national["source_notes"])
    sector = next(i for i in items if i["sector"])
    assert sector["sector"] == {"scheme": "ISIC", "version": "Rev.4", "code": "C", "native": "ECO_ISIC4_C",
                                "label": "Manufacturing"}
    assert sector["observations"][0]["unit_multiplier"] == "3"
    assert all(r["labour_release"]["release_basis"] == "retrieval_time" for r in records)


def test_oecd_keeps_adjustment_measure_and_unit_as_published():
    items = [r["labour_item"] for page in h.fetch("oecd") for r in page]
    assert {(i["area"]["code"], i["seasonal_adjustment"]) for i in items} == {
        ("DEU", "SA"), ("DEU", "NSA"), ("USA", "SA"), ("USA", "NSA")}
    assert all(i["dimensions"]["MEASURE"] == "UNE_LF_M" and i["definition"]["basis"] == "oecd-harmonised"
               for i in items)
    release = h.fetch("oecd")[0][0]["labour_release"]
    assert release["structure"]["dataflow_version"] == "1.0"


def test_eurostat_flags_are_verbatim_and_confidential_cells_carry_no_value():
    records = [r for page in h.fetch("eurostat") for r in page]
    regional = {r["labour_item"]["area"]["code"]: r["labour_item"] for r in records
                if r["labour_item"]["indicator"]["code"] == "lfst_r_lfu3rt"}
    berlin = {o["period"]: o for o in regional["DE30"]["observations"]}
    assert berlin["2097"]["status"] == "confidential" and berlin["2097"]["value"] is None
    assert berlin["2098"]["flags"] == {"OBS_FLAG": "u"} and berlin["2098"]["value"] == "5.4"
    release = records[0]["labour_release"]
    assert release["release_basis"] == "provider_last_update" and release["published_on"] == "2099-03-20"
    esms = records[0]["labour_item"]["references"][0]
    assert esms["identifier"] == "lfsa_esms"


def test_bls_series_ids_decode_and_footnotes_stay_per_observation():
    assert decode_bls_series_id("CES3000000001")["industry"]["naics"] == "31-33"
    laus = decode_bls_series_id("LASST060000000000003")
    assert laus["area"]["fips_state"] == "06" and laus["data_type"]["label"] == "unemployment rate"
    oews = decode_bls_series_id("OEUN000000000000015125204")
    assert oews["occupation"]["code"] == "15-1252" and oews["data_type"]["label"] == "annual mean wage"
    jolts = decode_bls_series_id("JTS000000000000000JOL")
    assert jolts["data_type"]["label"] == "job openings" and jolts["seasonal_adjustment"] == "SA"
    with pytest.raises(ValueError):
        decode_bls_series_id("XX123")
    items = {r["labour_item"]["native_key"]: r for page in h.fetch("bls") for r in page}
    ces = {o["period"]: o for o in items["CES3000000001"]["labour_item"]["observations"]}
    assert ces["2099-02"]["footnotes"] == [{"code": "P", "text": "preliminary"}]
    jolts_obs = {o["period"]: o for o in items["JTS000000000000000JOL"]["labour_item"]["observations"]}
    assert jolts_obs["2099-02"]["status"] == "not_published" and jolts_obs["2099-02"]["value"] is None
    # The registration key is sent to the transport but never recorded.
    for record in items.values():
        assert "registrationkey" not in record["url"] and FIXTURE_SECRET not in json.dumps(record)


def test_bls_requires_its_key_and_stops_on_the_daily_threshold():
    item = h.source("bls")
    adapter = LabourStatisticsAdapter(item, transport=fixture_transport(h.pages("bls")))
    with pytest.raises(SourcePackError) as refused:
        adapter.fetch_page({"operation": "release", "parameters": {}}, cursor=None)
    assert refused.value.code == "authentication_failed"
    threshold = [dict(p, body=json.dumps({"status": "REQUEST_NOT_PROCESSED", "message": [
        "daily threshold for total number of requests allocated to API key was reached"]})) for p in h.pages("bls")]
    with pytest.raises(SourcePackError) as limited:
        h.fetch("bls", transport=fixture_transport(threshold))
    assert limited.value.code == "rate_limited"


def test_undeclared_parameters_and_foreign_hosts_are_refused():
    item = h.source("oecd")
    adapter = LabourStatisticsAdapter(item, transport=fixture_transport(h.pages("oecd")))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "release", "parameters": {"key": "X"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    broken = json.loads(json.dumps(item))
    broken["endpoint"] = "https://example.org/rest"
    with pytest.raises(SourcePackError):
        LabourStatisticsAdapter(broken)
