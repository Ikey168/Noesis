"""Extractives source contracts, declarations and the EITI, USGS and BGS parsers (#2657, #2667, #2672, #2677)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.extractives_sources import (
    BOUNDED_COVERAGE,
    LIVE_VERIFICATION,
    MINIMISATION,
    PROVIDER_CONTRACTS,
    REDACTED_NAME,
    ExtractivesAdapter,
    extractives_declaration,
    fixture_transport,
    parse_bgs,
    parse_eiti,
    parse_usgs,
    personal_keys,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import extractives_harness as h


def test_every_provider_has_an_audited_contract_and_is_unverified_live():
    assert set(PROVIDER_CONTRACTS) == {"eiti", "usgs-mcs", "bgs-wms"}
    for provider, contract in PROVIDER_CONTRACTS.items():
        for key in ("access", "entry_points", "authentication", "rate_limits", "terms", "revision_model", "read",
                    "verify"):
            assert contract[key], (provider, key)
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        # Pages that could not be read are cited with the date and marked unverified.
        assert all("2026-09-30" in note and "unverified" in note for note in contract["read"].values())
    assert "Open Government Licence" in PROVIDER_CONTRACTS["bgs-wms"]["terms"]
    assert "W withheld" in PROVIDER_CONTRACTS["usgs-mcs"]["value_conventions"]
    assert {"stored", "excluded", "redacted", "retention", "access"} <= set(MINIMISATION)
    assert BOUNDED_COVERAGE["record_cap"]


def test_the_source_pack_declares_three_sources_with_pinned_fixtures_that_replay_offline():
    manifest = h.manifest()
    # 1.7.0 adds the extractives features' EITI, USGS and BGS sources (#2653); earlier sources are kept verbatim.
    assert manifest["version"] == "1.7.0"
    sources = [s for s in manifest["sources"] if s["connector"] == "extractives"]
    assert {s["source_id"] for s in sources} == set(h.SOURCES.values())
    assert {s["extractives"]["live_verification"] for s in sources} == {"unverified-live"}
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": sources})
    assert replay["valid"] and replay["coverage"]["verified"] == 3


def test_declarations_refuse_foreign_hosts_and_undated_commodity_publications():
    item = h.source("usgs")
    bad = json.loads(json.dumps(item))
    bad["endpoint"] = "https://example.org/data"
    with pytest.raises(SourcePackError):
        extractives_declaration(bad)
    undated = json.loads(json.dumps(item))
    del undated["extractives"]["documents"][0]["release"]
    with pytest.raises(SourcePackError, match="publication"):
        extractives_declaration(undated)


def test_eiti_keeps_reporters_apart_with_discrepancies_and_drops_personal_fields():
    document = h.source("eiti")["extractives"]["documents"][1]
    raw = h.pages("eiti")[1]["body"].encode()
    release = parse_eiti(raw, document=document)
    (item,) = release["items"]
    assert release["release_basis"] == "report_publication" and release["published_on"] == "2024-06-30"
    by = {(line["company"]["name_as_reported"], line["reported_by"]): line for line in item["company_payments"]}
    assert by[("Exampla Intermediate B.V.", "company")]["amount_text"] == "800000.00"
    assert by[("Exampla Intermediate B.V.", "government")]["amount_text"] == "790000.00"
    assert by[("EXAMPLA UK LIMITED", "company")]["currency"] == "USD"
    assert by[("Exampla Intermediate B.V.", "company")]["project"]["identifiers"]
    (disc,) = item["discrepancies"]
    assert disc["discrepancy_text"] == "-10000.00" and disc["explanation"].startswith("timing difference")
    # EX01: contact persons excluded, the natural-person entity redacted without identifiers.
    assert personal_keys(item) == [] and "$.contact" in item["excluded_fields"]
    person = by[(REDACTED_NAME, "company")]["company"]
    assert person["redacted"] and person["identifiers"] == [] and "Juan" not in json.dumps(item)
    assert item["government_revenues"][0]["revenue_stream"]["gfs_code"] == "1141E1"


def test_usgs_keeps_withheld_values_estimates_and_units_per_series():
    item = h.source("usgs")
    release = parse_usgs(h.pages("usgs")[0]["body"].encode(), document=item["extractives"]["documents"][0])
    series = {(i["commodity"]["name"], i["statistic"], i["country"]["name"]): i for i in release["items"]}
    assert ("Gold", "production", "Peru") not in series  # outside the bounded commodities
    lithium_us = {o["period"]: o for o in series[("Lithium", "production", "United States")]["observations"]}
    assert lithium_us["2023"]["status"] == "withheld" and lithium_us["2023"]["value"] is None
    assert "company proprietary" in lithium_us["2023"]["notes"][0]
    chile = {o["period"]: o for o in series[("Lithium", "production", "Chile")]["observations"]}
    assert chile["2023"]["value"] == "49000" and chile["2023"]["estimated"] and chile["2022"]["value"] == "44000"
    copper = series[("Copper", "production", "Peru")]
    assert copper["unit"] == {"label": "thousand metric tons"}
    assert copper["commodity"]["form"] == "Mine production, recoverable copper content"
    assert {o["period"]: o["estimated"] for o in copper["observations"]} == {"2022": False, "2023": True}


def test_bgs_page_is_one_publication_with_attribution_and_never_truncated():
    item = h.source("bgs")
    document = item["extractives"]["documents"][0]
    release = parse_bgs(h.pages("bgs")[0]["body"].encode(), document=document)
    assert release["structure"]["attribution"] == "Contains British Geological Survey materials (c) UKRI 2024"
    stats = {(i["statistic"], i["country"]["name"]) for i in release["items"]}
    assert ("exports", "Peru") in stats and ("production", "Chile") in stats
    assert all(i["country"]["code_scheme"] == "iso3166-1-alpha3" for i in release["items"])
    truncated = json.loads(h.pages("bgs")[0]["body"])
    truncated["numberMatched"] = 99
    page = [{**h.pages("bgs")[0], "body": json.dumps(truncated)}]
    adapter = ExtractivesAdapter(h.source("bgs", slice(0, 1)), transport=fixture_transport(page))
    with pytest.raises(SourcePackError) as raised:
        adapter.fetch_page({"operation": "release", "parameters": {}}, cursor=None)
    assert raised.value.code == "budget_exhausted"


def test_adapter_pages_carry_the_release_header_and_receipt():
    item = h.source("eiti")
    pages = h.fetch(item, h.pages("eiti"))
    assert len(pages) == 2
    header = pages[0][0]["extractives_release"]
    assert header["evidence_origin"] == "fixture" and header["live_verification"] == "unverified-live"
    assert header["licence"]["terms"] and "contact@example.invalid" not in json.dumps(pages)
