"""Tax transparency acquisition: audited contracts, declarations, the CSV adapter and the pinned fixtures (#2889)."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion.source_packs import SourcePackConformance, SourcePackError, validate_source_pack
from src.ingestion.tax_sources import (
    CAPS,
    DATASETS,
    LIVE_VERIFICATION,
    NOT_IMPLEMENTED,
    PROVIDER_CONTRACTS,
    PROVIDERS,
    REFERENCE_ONLY,
    TaxTransparencyAdapter,
    fixture_transport,
)
from tests.unit import tax_harness as h


def test_every_provider_has_an_audited_contract_and_a_live_verification_entry():
    audit = (h.ROOT / "docs/development/tax-transparency-evidence/source-audit.md").read_text()
    for provider in (*PROVIDERS, *NOT_IMPLEMENTED, *REFERENCE_ONLY):
        assert provider in PROVIDER_CONTRACTS and provider in LIVE_VERIFICATION
        assert PROVIDER_CONTRACTS[provider]["status"] == LIVE_VERIFICATION[provider]["status"]
    assert {LIVE_VERIFICATION[p]["status"] for p in PROVIDERS} == {"unverified-live"}
    assert {LIVE_VERIFICATION[p]["status"] for p in NOT_IMPLEMENTED} == {"not-implemented"}
    assert LIVE_VERIFICATION["public-cbcr"]["status"] == "reference-only"
    for provider in PROVIDERS:
        assert f"`{provider}`" in audit
    for dataset in ("DSD_CBCR@DF_CBCRI", "DSD_TAX_CIT@DF_CIT", "DSD_ETR@DF_ETR"):
        assert dataset in audit and dataset in DATASETS
    # Licences are never invented: the EU Tax Observatory terms are recorded as not established.
    assert "not established" in PROVIDER_CONTRACTS["eutax-offshore-wealth"]["licence"]["text"]
    assert "non-commercial" in PROVIDER_CONTRACTS["tjn-cthi"]["licence"]["text"]


def test_the_pack_validates_and_its_fixtures_replay_offline_through_the_real_adapter():
    manifest = h.manifest()
    assert manifest["pack_id"] == "economic-tax-transparency" and manifest["version"] == "1.0.0"
    assert {s["source_id"] for s in manifest["sources"]} == set(PROVIDERS)
    assert all(s["tax_transparency"]["live_verification"] == "unverified-live" for s in manifest["sources"])
    eutax = [s for s in manifest["sources"] if s["source_id"].startswith("eutax-")]
    assert all("not established" in s["license"]["redistribution"] for s in eutax)
    replay = SourcePackConformance(h.ROOT).offline(manifest)
    assert replay["valid"] and replay["coverage"] == {"configured": 4, "verified": 4}


def test_cbcr_items_keep_partner_groupings_status_letters_and_confidential_cells():
    (records,) = h.fetch("cbcr")
    items = [r["tax_item"] for r in records]
    assert len(items) == 18 and {i["nature"] for i in items} == {"published_statistic"}
    header = records[0]["tax_release"]
    assert header["edition"].startswith("Corporate Tax Statistics 2024") and header["published_on"] == "2024-07-15"
    assert header["release_basis"] == "declared_release" and header["evidence_origin"] == "fixture"
    total = next(i for i in items if i["native_key"].startswith("DSD_CBCR@DF_CBCRI.DEU._T.PBT"))
    assert total["counterpart"]["code"] == "_T" and total["unit"] == {"code": "USD", "label": "US dollars",
                                                                       "multiplier": "6"}
    cells = {o["period"]: o for o in total["observations"]}
    assert cells["2092"]["flags"] == {"CONF_STATUS": "F", "OBS_STATUS": "P"} and cells["2092"]["status"] == "reported"
    assert any(n["kind"] == "provisional" for n in total["source_notes"])
    secret = next(i for i in items if i["native_key"].startswith("DSD_CBCR@DF_CBCRI.NLD.FOR.ITA"))
    cell = next(o for o in secret["observations"] if o["period"] == "2092")
    assert cell["status"] == "confidential" and cell["value"] is None
    assert total["method"] is None and total["definition"]["caveats"]


def test_model_estimates_carry_method_and_edition_and_published_rates_do_not():
    pages = h.fetch("rates")
    items = [r["tax_item"] for page in pages for r in page]
    cit = [i for i in items if i["dataset"] == "DSD_TAX_CIT@DF_CIT"]
    etr = [i for i in items if i["dataset"] == "DSD_ETR@DF_ETR"]
    assert len(cit) == 3 and len(etr) == 6
    assert {i["nature"] for i in cit} == {"published_statistic"} and all(i["method"] is None for i in cit)
    assert {i["nature"] for i in etr} == {"model_estimate"}
    assert all(i["method"]["reference"].startswith("https://") and i["edition"] for i in etr)
    assert {o["status"] for i in etr for o in i["observations"]} == {"estimated"}
    (offshore,) = h.fetch("offshore")
    wide = [r["tax_item"] for r in offshore]
    assert {i["indicator"]["code"] for i in wide} == {"OFW_USD", "OFW_GDP"}
    assert {i["unit"]["code"] for i in wide} == {"USD", "PC_GDP"}
    (billionaire,) = h.fetch("billionaire")
    nld_income = next(r["tax_item"] for r in billionaire if r["tax_item"]["native_key"].startswith(
        "gter-billionaire-effective-tax-rates.NLD._Z.BETR_I"))
    assert nld_income["observations"][0]["status"] == "not_published"
    assert nld_income["observations"][0]["value"] is None


def _item(name: str = "cbcr") -> dict:
    return copy.deepcopy(h.source(name))


def test_declarations_refuse_unaudited_datasets_other_hosts_credentials_and_unbounded_periods():
    item = _item()
    item["tax_transparency"]["documents"][0]["dataset"] = "DSD_CBCR@DF_CBCRVI"
    with pytest.raises(SourcePackError, match="not an audited dataset"):
        TaxTransparencyAdapter(item)
    item = _item()
    item["tax_transparency"]["documents"][0]["url"] = "https://example.org/cbcr.csv"
    with pytest.raises(SourcePackError, match="endpoint's host"):
        TaxTransparencyAdapter(item)
    item = _item()
    item["tax_transparency"]["documents"][0]["period"] = {"start": "2081", "end": "2093"}
    with pytest.raises(SourcePackError, match="at most 5 annual periods"):
        TaxTransparencyAdapter(item)
    item = _item("offshore")
    del item["tax_transparency"]["documents"][0]["method"]
    with pytest.raises(SourcePackError, match="states its method"):
        TaxTransparencyAdapter(item)
    item = _item("rates")
    item["tax_transparency"]["documents"].append(copy.deepcopy(item["tax_transparency"]["documents"][0]))
    item["budgets"]["max_pages"] = 3
    with pytest.raises(SourcePackError, match="at most 2 documents"):
        TaxTransparencyAdapter(item)
    raw = json.loads(h.PACK_PATH.read_text())
    raw["sources"][0]["auth"] = {"kind": "required-secret", "secret_ref": "NOESIS_TAX_KEY"}
    with pytest.raises(SourcePackError, match="no credential"):
        TaxTransparencyAdapter(next(s for s in validate_source_pack(raw)["sources"]
                                    if s["source_id"] == raw["sources"][0]["source_id"]))
    assert CAPS["oecd-cts-cbcr"]["series_per_response"] == 40


def test_drift_undeclared_rows_and_other_hosts_fail_the_document_without_partial_output():
    def page_with(body: str, **extra):
        page = copy.deepcopy(h.pages("cbcr")[0])
        page["body"] = body
        page.update(extra)
        return [page]

    original = h.pages("cbcr")[0]["body"]
    with pytest.raises(SourcePackError) as drift:
        h.fetch("cbcr", transport=fixture_transport(page_with(original.replace("OBS_VALUE", "VALUE"))))
    assert drift.value.code == "schema_drift"
    with pytest.raises(SourcePackError) as undeclared:
        h.fetch("cbcr", transport=fixture_transport(page_with(original.replace(",NLD,", ",LUX,", 1))))
    assert undeclared.value.code == "schema_drift" and "LUX" in str(undeclared.value)
    with pytest.raises(SourcePackError) as moved:
        h.fetch("cbcr", transport=fixture_transport(page_with(original, final_url="https://example.org/x.csv")))
    assert moved.value.code == "network_policy"
    with pytest.raises(SourcePackError) as unavailable:
        h.fetch("cbcr", transport=fixture_transport(page_with("", status=503)))
    assert unavailable.value.code == "source_unavailable"
