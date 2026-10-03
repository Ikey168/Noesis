"""IB03-IB05 (#2738): business statistics source contracts and acquisition (Eurostat STS, business demography, CBP)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.business_statistics_sources import (
    BOUNDED_COVERAGE,
    CAPS,
    EXCLUSIONS,
    FIXTURE_SECRET,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    SECRET_REF,
    BusinessStatisticsAdapter,
    fixture_transport,
    index_base_year,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import business_statistics_harness as h


def test_every_provider_has_the_audited_contract_caps_and_an_unverified_live_entry():
    assert set(PROVIDER_CONTRACTS) == set(h.SOURCES.values()) == set(LIVE_VERIFICATION) == set(CAPS)
    for provider, contract in PROVIDER_CONTRACTS.items():
        for key in ("publisher", "access", "authentication", "licence", "redistribution", "rate_limits",
                    "revision_model", "definitions", "statistical_unit", "unavailable_fallback"):
            assert contract[key], (provider, key)
        assert contract["status"] == LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    assert SECRET_REF == "NOESIS_CENSUS_API_KEY" and SECRET_REF in PROVIDER_CONTRACTS["us-census-cbp"]["authentication"]
    assert CAPS["eurostat-sts"] == {"documents": 1, "series_per_response": 10, "months": 36}
    assert CAPS["eurostat-business-demography"]["series_per_response"] == 20
    assert CAPS["us-census-cbp"] == {"documents": 2, "rows_per_response": 50, "years": 2}
    assert BOUNDED_COVERAGE["eurostat-sts"]["places"] == {"Germany": "geo DE"}
    assert BOUNDED_COVERAGE["us-census-cbp"]["places"] == {"California": "state 06"}
    assert {"re-basing indices to another base year", "blending Eurostat and Census figures",
            "reconstructing suppressed, withheld or noise-infused cells"} <= set(EXCLUSIONS)
    audit = (h.ROOT / "docs/development/business-statistics-evidence/source-audit.md").read_text()
    for name in ("sts_inpr_m", "bd_9bd_sz_cl_r2", "County Business Patterns", "LIVE_VERIFICATION", "#2738",
                 "Deviation"):
        assert name in audit


def test_the_pinned_fixtures_replay_offline_through_the_real_adapter():
    manifest = h.manifest()
    assert manifest["version"] == "1.7.0"  # the business-statistics sources (#2738)
    business = [s for s in manifest["sources"] if s["connector"] == "business-statistics"]
    assert {s["source_id"] for s in business} == set(h.SOURCES.values())
    assert {s["mapping"]["target_schema"] for s in business} == {"noesis-business-statistics-record-v2"}
    assert {s["business_statistics"]["live_verification"] for s in business} == {"unverified-live"}
    cbp = h.source("cbp")
    assert cbp["auth"] == {"kind": "optional-secret", "secret_ref": "NOESIS_CENSUS_API_KEY"}
    assert h.source("sts")["auth"] == {"kind": "none"}
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": business})
    assert replay["valid"] and replay["coverage"]["verified"] == 3


def test_sts_keeps_adjustment_and_base_year_in_the_key_and_flags_verbatim():
    records = [r for page in h.fetch("sts") for r in page]
    items = [r["business_item"] for r in records]
    assert {(i["classification"]["code"], i["adjustment"]) for i in items} == {
        ("B-D", "SCA"), ("B-D", "NSA"), ("C", "SCA"), ("C", "NSA")}
    assert {i["unit"]["code"] for i in items} == {"I21"} and index_base_year("I21") == "2021"
    c_sca = next(i for i in items if i["classification"]["code"] == "C" and i["adjustment"] == "SCA")
    cells = {o["period"]: o for o in c_sca["observations"]}
    assert cells["2096-03"]["flags"] == {"OBS_FLAG": "b"} and cells["2096-03"]["flag_meanings"] == [
        "break in time series"]
    assert cells["2096-04"]["flags"] == {"OBS_FLAG": "p"} and cells["2096-04"]["value"] == "104.4"
    assert any(n["kind"] == "break" and n["periods"] == ["2096-03"] for n in c_sca["source_notes"])
    header = records[0]["business_release"]
    assert header["release_basis"] == "provider_last_update" and header["published_on"] == "2024-03-15"
    assert header["live_verification"] == "unverified-live" and header["evidence_origin"] == "fixture"
    assert header["document_key"] == "eurostat-sts:sts_inpr_m:M.PROD.B-D+C.SCA+NSA..DE"


def test_business_demography_keeps_provisional_deaths_and_confidential_cells():
    items = {r["business_item"]["indicator"]["code"]: r["business_item"] for page in h.fetch("bd") for r in page}
    assert {i["indicator"]["concept"] for i in items.values()} == {"active_enterprises", "enterprise_births",
                                                                   "enterprise_deaths"}
    deaths = {o["period"]: o for o in items["V11930"]["observations"]}
    assert deaths["2095"]["flags"] == {"OBS_FLAG": "p"}
    births = {o["period"]: o for o in items["V11920"]["observations"]}
    assert births["2095"]["status"] == "confidential" and births["2095"]["value"] is None
    assert items["V11910"]["statistical_unit"] == "enterprise"


def test_cbp_flags_withheld_cells_naics_vintages_and_the_key_never_recorded():
    pages = h.fetch("cbp")
    assert len(pages) == 2  # one document per reference year
    items = {(r["business_item"]["classification"]["version"], r["business_item"]["classification"]["code"],
              r["business_item"]["indicator"]["code"]): r for page in pages for r in page}
    assert {k[0] for k in items} == {"2017", "2022"}
    withheld = items[("2017", "31-33", "PAYANN")]["business_item"]["observations"][0]
    assert withheld["status"] == "withheld" and withheld["value"] is None and withheld["value_text"] == "0"
    noisy = items[("2017", "00", "EMP")]["business_item"]["observations"][0]
    assert noisy["flags"] == {"EMP_N": "G"} and noisy["attributes"]["exact"] is False
    estab = items[("2017", "00", "ESTAB")]["business_item"]["observations"][0]
    assert estab["flags"] == {} and estab["value"] == "900100"
    release = items[("2022", "00", "EMP")]["business_release"]
    assert release["release_basis"] == "declared_release" and release["published_on"] == "2024-06-27"
    assert release["dataflow_version"] == "NAICS2022 (2097)"
    # The optional key is sent to the transport but never recorded.
    sent = []

    def spy(**kwargs):
        sent.append(dict(kwargs["params"]))
        return fixture_transport(h.pages("cbp"))(**kwargs)

    for page in h.fetch("cbp", transport=spy):
        for record in page:
            assert "key=" not in record["url"] and FIXTURE_SECRET not in json.dumps(record)
    assert all(params.get("key") == FIXTURE_SECRET for params in sent)
    keyless = [r for page in h.fetch("cbp", secret=None) for r in page]
    assert [r["business_item"] for r in keyless] == [r["business_item"] for page in pages for r in page]


def test_eurostat_requests_never_carry_a_credential_and_undeclared_controls_are_refused():
    sent = []

    def spy(**kwargs):
        sent.append(dict(kwargs["params"]))
        return fixture_transport(h.pages("sts"))(**kwargs)

    h.fetch("sts", transport=spy, secret="should-not-be-sent")
    assert all("key" not in params for params in sent)
    adapter = BusinessStatisticsAdapter(h.source("bd"), transport=fixture_transport(h.pages("bd")))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "release", "parameters": {"key": "X"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    broken = json.loads(json.dumps(h.source("cbp")))
    broken["endpoint"] = "https://example.org/data"
    with pytest.raises(SourcePackError):
        BusinessStatisticsAdapter(broken)
    unbounded = json.loads(json.dumps(h.source("cbp")))
    unbounded["business_statistics"]["documents"][0]["naics_codes"] = ["*"]
    with pytest.raises(SourcePackError):
        BusinessStatisticsAdapter(unbounded)
    too_long = json.loads(json.dumps(h.source("sts")))
    too_long["business_statistics"]["documents"][0]["params"]["endPeriod"] = "2099-12"
    with pytest.raises(SourcePackError):
        BusinessStatisticsAdapter(too_long)


def test_failures_are_reported_never_truncated_or_marked_removed():
    rate_limited = [dict(p, status=429, headers={"Retry-After": "60"}) for p in h.pages("cbp")]
    with pytest.raises(SourcePackError) as limited:
        h.fetch("cbp", transport=fixture_transport(rate_limited))
    assert limited.value.code == "rate_limited"
    drift = [dict(p, body=json.dumps([["NAME", "ESTAB"], ["California", "1"]])) for p in h.pages("cbp")]
    with pytest.raises(SourcePackError) as drifted:
        h.fetch("cbp", transport=fixture_transport(drift))
    assert drifted.value.code == "schema_drift"
    item = json.loads(json.dumps(h.source("sts")))
    item["budgets"]["max_results"] = 2
    with pytest.raises(SourcePackError) as budget:
        h.fetch("sts", item=item)
    assert budget.value.code == "budget_exhausted"

    def moved(**kwargs):
        return {**fixture_transport(h.pages("bd"))(**kwargs), "final_url": "https://example.org/x"}

    with pytest.raises(SourcePackError) as redirected:
        h.fetch("bd", transport=moved)
    assert redirected.value.code == "network_policy"
