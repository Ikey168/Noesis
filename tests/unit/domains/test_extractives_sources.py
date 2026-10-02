"""Extractives source audit and EITI/USGS/BGS acquisition (#2653: EX01 #2657, EX03 #2667, EX04 #2672, EX05 #2677)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft7Validator

from src.ingestion.extractives_sources import (
    BOUNDED_COVERAGE,
    FEATURES,
    LIVE_VERIFICATION,
    MINIMISATION,
    PROVIDER_CONTRACTS,
    WITHHELD_NAME,
    ExtractivesAdapter,
    ExtractivesFormatError,
    check_document,
    coverage_report,
    fixture_transport,
    parse_usgs,
    personal_keys,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.extractives_store import ExtractivesStore
from tests.unit import extractives_harness as h

SCHEMA = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-extractives-record-v2.json").read_text())


def test_every_provider_has_a_recorded_contract_minimisation_decision_and_bounded_coverage():
    assert set(PROVIDER_CONTRACTS) == {"eiti", "usgs-mcs", "bgs-wms"}
    for provider, contract in PROVIDER_CONTRACTS.items():
        for key in ("access", "format", "entry_points", "authentication", "identifiers", "terms", "licence",
                    "rate_limits", "revision_model", "temporal_semantics", "personal_data"):
            assert contract.get(key), (provider, key)
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert provider in BOUNDED_COVERAGE
    assert set(FEATURES.values()) == {"extractives-eiti", "extractives-usgs", "extractives-bgs"}
    for key in ("stored", "excluded", "redacted", "retention", "access", "personal_keys"):
        assert MINIMISATION[key], key
    assert coverage_report()["not_implemented"] == []
    doc = (h.ROOT / "docs/development/extractives-evidence/source-audit.md").read_text()
    for needle in ("Access decisions", "Per-source contract", "minimisation decision", "Bounded coverage",
                   "_verify_", "not re-verified live", "Retention", "Who may query", "economic.json"):
        assert needle in doc, needle


def test_the_separate_source_pack_validates_and_its_pinned_fixtures_replay_offline():
    manifest = h.manifest()
    assert manifest["pack_id"] == "economic-extractives" and manifest["domains"] == ["economic"]
    assert {s["source_id"] for s in manifest["sources"]} == set(h.SOURCES.values())
    assert {s["extractives"]["live_verification"] for s in manifest["sources"]} == {"unverified-live"}
    report = SourcePackConformance(h.ROOT).offline(json.loads(h.PACK_PATH.read_text()))
    assert report["valid"] and report["coverage"]["verified"] == 3
    economic = json.loads((h.ROOT / "config/source_packs/economic.json").read_text())
    assert not [s for s in economic["sources"] if s.get("connector") == "extractives"]


def test_declarations_are_checked_and_undeclared_controls_and_truncation_are_refused():
    document = json.loads(json.dumps(h.source("eiti")["extractives"]["documents"][0]))
    del document["fiscal_period"]
    with pytest.raises(ExtractivesFormatError):
        check_document("eiti-summary-json", document)
    source = h.source("usgs")
    source["extractives"]["documents"][0]["url"] = "https://example.org/x.csv"
    with pytest.raises(SourcePackError) as caught:
        ExtractivesAdapter(source)
    assert caught.value.code == "invalid_manifest"
    adapter = ExtractivesAdapter(h.source("eiti"), transport=fixture_transport(h.pages("eiti")))
    with pytest.raises(SourcePackError) as refused:
        adapter.fetch_page({"operation": "release", "parameters": {"country": "NO"}}, cursor=None)
    assert refused.value.code == "parameter_forbidden"
    with pytest.raises(SourcePackError) as budget:
        adapter.fetch_page({"operation": "release", "parameters": {}, "limit": 3}, cursor=None)
    assert budget.value.code in {"budget_exhausted", "response_too_large"}
    limited = [{**p, "status": 429, "headers": {"Retry-After": "60"}, "body": ""} for p in h.pages("usgs")]
    with pytest.raises(SourcePackError) as rate:
        ExtractivesAdapter(h.source("usgs"), transport=fixture_transport(limited)).fetch_page(
            {"operation": "release", "parameters": {}}, cursor=None)
    assert rate.value.code == "rate_limited"


def test_eiti_report_versions_keep_government_and_company_figures_discrepancies_projects_and_receipts():
    pages = h.fetch("eiti")
    header = pages[0][0]["extractives_release"]
    assert header["report_key"] == h.NL_REPORT and header["release_version"] == "1"
    assert header["evidence_origin"] == "fixture" and header["live_verification"] == "unverified-live"
    assert set(header["structure"]["personal_fields_dropped"]) >= {"contact", "email", "beneficial_owners"}
    items = {r["extractives_item"]["record_key"]: r["extractives_item"] for r in pages[0]}
    assert all(personal_keys(item) == [] for item in items.values())
    payment = items[h.CIT_PAYMENT]
    assert payment["government_reported"] == {"value_text": "1200000", "value": "1200000", "currency": "EUR"}
    assert payment["company_reported"]["value"] == "1250000"
    assert payment["discrepancy_as_published"] == {"value_text": "-50000", "value": "-50000", "currency": "EUR",
                                                   "explanation": "timing difference, as stated in the report"}
    assert payment["project_key"] == f"{h.NL_REPORT}:project:p1"
    project = items[f"{h.NL_REPORT}:project:p1"]
    assert project["name_as_published"] == "Exampla Gas Field A (fixture)"
    assert project["licences"][0]["number"] == "NL-WIN-0001" and project["company_key"] == h.INT_COMPANY
    individual = next(i for i in items.values() if i.get("natural_person"))
    assert individual["name_as_published"] == WITHHELD_NAME and individual["identifiers"] == []
    assert "Fixture-Person" not in json.dumps(pages)
    bonus = next(i for k, i in items.items() if k.endswith("signature-bonus"))
    assert bonus["government_reported"]["currency"] == "USD"  # currency as reported, never converted
    conn = h.connection()
    result = h.apply(conn, "eiti", retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert [r["revisions"]["created"] for r in result] == [15, 10]
    store = ExtractivesStore(conn)
    for view in store.records(h.NS):
        assert not list(Draft7Validator(SCHEMA).iter_errors(view)), view["record_key"]
        assert view["as_of"] and view["observed_at"] and view["release_id"]


def test_usgs_releases_are_vintages_with_estimated_revised_and_withheld_values_as_published():
    conn = h.connection()
    h.apply(conn, "usgs", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = ExtractivesStore(conn)
    (us_lithium,) = store.find_series(h.NS, provider="usgs-mcs", commodity="lithium", statistic="production",
                                      country="US")
    values = store.values(h.NS, us_lithium["current_vintage_id"])
    assert [(v["period"], v["status"], v["value"], v["value_text"]) for v in values] == [
        ("2022", "withheld", None, "W"), ("2023", "withheld", None, "W")]
    (chile,) = store.find_series(h.NS, provider="usgs-mcs", commodity="copper", statistic="production", country="CL")
    assert chile["unit"] == "metric tons, copper content" and chile["country"]["code_basis"] == "operator-declared"
    assert chile["commodity"]["definition"]["reference"].startswith("https://www.usgs.gov/")
    assert [(v["period"], v["estimated"]) for v in store.values(h.NS, chile["current_vintage_id"])] == [
        ("2022", False), ("2023", True)]
    world = [s for s in store.find_series(h.NS, commodity="copper") if s["country"]["aggregate"]]
    assert world and all(s["country"]["iso2"] is None for s in world)
    for series in store.find_series(h.NS):
        assert not list(Draft7Validator(SCHEMA).iter_errors(series)), series["series_id"]
    # Numbers live in the Economics series storage.
    row = conn.execute("SELECT provider, geography FROM dataset_series WHERE series_id=?",
                       [chile["series_id"]]).fetchone()
    assert row == ("extractives:usgs-mcs", "iso2:CL")
    # The next annual release is a new vintage; the revised 'r' marker and the estimate are stored as published.
    h.apply(conn, "usgs", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    vintages = store.vintage_rows(h.NS, chile["series_id"])
    assert len(vintages) == 2 and vintages[1]["revision_of"] == vintages[0]["vintage_id"]
    later = store.values(h.NS, vintages[1]["vintage_id"])
    assert [(v["period"], v["value"], v["revised"], v["estimated"]) for v in later] == [
        ("2023", "5250000", True, False), ("2024", "5300000", False, True)]
    # Unknown country rows are refused rather than guessed.
    document = h.source("usgs")["extractives"]["documents"][0]
    with pytest.raises(ExtractivesFormatError):
        parse_usgs(b"Country,Prod_t_2022,Prod_t_est_2023,Reserves_t,Reserves_notes\nAtlantis,1,2,3,\n",
                   document=document)


def test_bgs_publications_are_vintages_kept_apart_from_usgs_with_attribution():
    conn = h.connection()
    h.apply(conn, "usgs", retrieved_at_ms=h.FIRST_RETRIEVAL)
    h.apply(conn, "bgs", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = ExtractivesStore(conn)
    chile = store.find_series(h.NS, commodity="copper", statistic="production", country="CL")
    assert sorted(s["provider"] for s in chile) == ["bgs-wms", "usgs-mcs"]  # separate series, never one
    bgs = next(s for s in chile if s["provider"] == "bgs-wms")
    assert "British Geological Survey" in bgs["licence"]["attribution"]
    trade = {s["statistic"] for s in store.find_series(h.NS, provider="bgs-wms", country="DE", commodity="copper")}
    assert trade == {"imports", "exports"}
    (nl_oil,) = store.find_series(h.NS, commodity="crude-petroleum", country="NL")
    assert nl_oil["commodity"]["siec"] == "O4100_TOT"
    assert [(v["period"], v["status"]) for v in store.values(h.NS, nl_oil["current_vintage_id"])] == [
        ("2021", "reported"), ("2022", "not_available")]
    h.apply(conn, "bgs", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert store.series(h.NS, bgs["series_id"])["vintage_count"] == 2
    (peru,) = store.find_series(h.NS, provider="bgs-wms", commodity="copper", country="PE")
    assert peru["vintage_count"] == 1  # unchanged in the later publication: no new vintage
    citation = store.source_revision(h.NS, store.vintage_rows(h.NS, bgs["series_id"])[1]["release_id"])
    assert citation["release_version"] == "2019-2023" and citation["published_on"] == "2025-03-14"
