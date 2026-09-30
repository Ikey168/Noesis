"""EN01 and EN03-EN07 (#2231, #2236, #2238, #2241, #2243, #2248): bounded, receipted acquisition per source."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.ingestion import energy_sources as es
from src.ingestion import environment_providers as envp
from src.ingestion.provider_execution import ProviderError
from src.ingestion.source_packs import SourcePackConformance, validate_source_pack
from src.kb.energy_market import market_refs, publish_prices
from src.kb.energy_store import EnergyStore
from tests.unit.energy import fixture_builder as fb
from tests.unit.energy.harness import NS, SCOPES, acquire, register_market_listing

ROOT = Path(__file__).resolve().parents[3]
PACK = json.loads((ROOT / "config/source_packs/energy.json").read_text())


def _series(conn, **filters):
    return EnergyStore(conn).series(NS, scopes=SCOPES, **filters)


def _latest(conn, series):
    store = EnergyStore(conn)
    return store.vintages(NS, series["series_id"], scopes=SCOPES)[-1]


# ------------------------------------------------------------------- EN01


def test_every_source_has_a_documented_access_decision_and_bounded_coverage():
    for provider, contract in es.PROVIDER_CONTRACTS.items():
        for key in ("decision", "documentation", "endpoints", "authentication", "terms", "attribution", "rate_limits",
                    "revisions", "unavailable_fallback"):
            assert contract.get(key), (provider, key)
        assert es.LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    assert "no second acquisition path" in es.PROVIDER_CONTRACTS["entsoe"]["decision"]
    assert es.PROVIDER_CONTRACTS["energy-charts"]["decision"].startswith("acquire public_power only")
    assert es.SECRETS["entsoe"] is envp.SECRETS["entsoe"]
    assert es.PROVIDER_HOSTS["entsoe"] is envp.PROVIDER_HOSTS["entsoe"]
    assert set(es.BOUNDED_COVERAGE["datasets"]) == {"generation_by_fuel", "load", "day_ahead_price", "installed_capacity",
                                                    "cross_border_flows", "energy_balances"}
    audit = (ROOT / "docs/roadmaps/energy-systems-source-audit.md").read_text()
    for provider in ("ENTSO-E", "EIA", "Ember", "Eurostat", "Energy-Charts"):
        assert provider in audit


def test_source_pack_records_the_decisions_and_replays_offline():
    pack = validate_source_pack(PACK)
    assert pack["domains"] == ["energy"]
    assert {s["energy"]["provider"] for s in pack["sources"]} == set(es.PROVIDER_CONTRACTS)
    for source in pack["sources"]:
        assert source["energy"]["access_decision"] == es.PROVIDER_CONTRACTS[source["energy"]["provider"]]["decision"]
        if source["energy"]["provider"] in es.SECRETS:
            assert source["auth"]["kind"] == "required-secret" and source["auth"]["secret_ref"].startswith("NOESIS_")
    result = SourcePackConformance(ROOT).offline(PACK)
    assert result["valid"] and result["coverage"]["configured"] == result["coverage"]["verified"] == 10


# ------------------------------------------------------------------- EN03


def test_entsoe_reuses_the_environment_adapter_for_all_energy_documents():
    steps = es.plan("entsoe", fb.ENTSOE_SELECTION)
    assert {s["url"] for s in steps} == {"https://web-api.tp.entsoe.eu/api"}
    assert {s["params"]["documentType"] for s in steps} == {"A75", "A65", "A44", "A68", "A71", "A11"}
    assert steps == [dict(step, provider="entsoe") for step in envp.plan("entsoe", fb.ENTSOE_SELECTION)]
    with pytest.raises(ProviderError) as refused:
        es.plan("entsoe", {**fb.ENTSOE_SELECTION, "documents": ["load-forecast"]})
    assert refused.value.code == "forecast_refused"
    with pytest.raises(ProviderError):
        es.plan("entsoe", {**fb.ENTSOE_SELECTION, "documents": ["unavailability"]})


def test_entsoe_generation_load_price_capacity_and_flows_are_keyed_and_receipted():
    conn = duckdb.connect()
    result = acquire(conn, "energy-entsoe")
    assert result["ok"] and len(result["receipts"]) == 9
    assert result["coverage"]["notes"]["entsoe_no_data"] == ["cross-border-flow:10YPL-AREA-----S:out_of_subject",
                                                              "cross-border-flow:10YPL-AREA-----S:into_subject"]
    by_type = {}
    for series in _series(conn, provider="entsoe"):
        by_type.setdefault(series["record_type"], []).append(series)
    assert {k: len(v) for k, v in by_type.items()} == {"generation": 2, "load": 1, "price": 1, "capacity": 3,
                                                       "cross_border_flow": 2}
    price = _latest(conn, by_type["price"][0])
    assert price["record"]["unit"] == "EUR/MWh" and price["release_key"] == "fixture-a44-0c9e77@1"
    assert [v["value"] for v in EnergyStore(conn).values(price["vintage_id"])] == ["85.12", "79.40", "-3.50", "12.05"]
    flows = {s["counterpart_code"]: s for s in by_type["cross_border_flow"]}
    assert set(flows) == {"10YFR-RTE------C"}
    directions = {s["facets"]["direction"] for s in by_type["cross_border_flow"]}
    assert directions == {"out_of_subject", "into_subject"}
    unit = next(s for s in by_type["capacity"] if s["subject"]["kind"] == "unit")
    record = _latest(conn, unit)["record"]
    assert unit["subject"]["code"] == "11WD2FIXTURE0001" and record["capacity"]["level"] == "unit"
    assert record["capacity"]["effective_from"] == "2025-12-31T23:00:00Z"
    receipt = EnergyStore(conn).receipt_row(price["receipt_id"])
    assert "securityToken" not in json.dumps(receipt) and receipt["execution"] == "injected"


def test_missing_token_is_a_receipted_failure_that_changes_nothing():
    conn = duckdb.connect()
    acquire(conn, "energy-entsoe")
    before = conn.execute("SELECT count(*) FROM energy_vintages").fetchone()[0]
    failed = acquire(conn, "entsoe_revision_2", secret=None)
    assert not failed["ok"] and {f["failure_code"] for f in failed["failures"]} == {"authentication_failed"}
    assert conn.execute("SELECT count(*) FROM energy_vintages").fetchone()[0] == before
    assert EnergyStore(conn).provider_state(NS, "entsoe")["stale"]


def test_day_ahead_prices_are_written_through_market_storage_with_attribution():
    conn = duckdb.connect()
    acquire(conn, "energy-entsoe")
    register_market_listing(conn)
    price = _latest(conn, _series(conn, record_type="price")[0])
    written = publish_prices(conn, NS, price["vintage_id"], listing_id="listing:de-lu-da",
                             entitlement_id="entitlement:entsoe", principal_id="analyst", scopes={"operator"})
    assert [b["value"] for b in written["written"]] == ["85.12", "79.40", "12.05"]
    assert written["refused"] == [{"start": "2026-09-24T02:00:00Z", "value": "-3.50", "reason":
                                   "negative price: noesis-market-bar-v1 requires prices >= 0; kept in the energy vintage"}]
    row = conn.execute("SELECT payload_json FROM market_price_bar_revisions ORDER BY bar_start_ms LIMIT 1").fetchone()
    bar = json.loads(row[0])
    assert bar["provider"] == "entsoe:entsoe:A44:-" and bar["close"] == 85.12 and bar["currency"] == "EUR"
    assert bar["source_refs"][0]["source_revision_id"] == price["vintage_id"]
    assert bar["source_refs"][0]["license_id"] == "entsoe-transparency-terms"
    assert market_refs(conn, NS, scopes=SCOPES)[0]["listing_id"] == "listing:de-lu-da"


# ------------------------------------------------------------------- EN04


def test_eia_routes_generation_demand_interchange_and_capacity():
    conn = duckdb.connect()
    for name in ("energy-eia-fuel-type", "energy-eia-region", "energy-eia-interchange", "energy-eia-capacity"):
        assert acquire(conn, name)["ok"]
    types = {(s["record_type"], s["native_id"]) for s in _series(conn, provider="eia")}
    assert ("load", "region-data:CISO:D") in types and ("generation", "fuel-type-data:CISO:SUN") in types
    flow = next(s for s in _series(conn, provider="eia", record_type="cross_border_flow") if s["counterpart_code"] == "BPAT")
    assert _latest(conn, flow)["record"]["values"][0]["value"] == "-1210"
    retired = next(s for s in _series(conn, provider="eia", record_type="capacity") if s["subject"]["code"] == "99901:G2")
    record = _latest(conn, retired)["record"]
    assert record["capacity"]["effective_to"] == "2026-07" and record["release"]["basis"] == "declared_release"
    assert [v["value"] for v in record["values"]] == ["50", None]
    step = es.plan("eia", fb.EIA_SELECTIONS["fuel-type-data"])[0]
    assert "api_key" not in step["params"]
    with pytest.raises(ProviderError) as forecast:
        es.plan("eia", {**fb.EIA_SELECTIONS["region-data"], "types": ["DF"]})
    assert forecast.value.code == "forecast_refused"


def test_eia_revisions_are_separate_vintages_dated_by_retrieval():
    conn = duckdb.connect()
    acquire(conn, "energy-eia-fuel-type")
    revised = acquire(conn, "eia_fuel_type_revised")
    assert revised["applied"] == {"series": 0, "vintages": 1, "unchanged": 1}
    solar = next(s for s in _series(conn, provider="eia") if s["native_id"].endswith(":SUN"))
    vintages = EnergyStore(conn).vintages(NS, solar["series_id"], scopes=SCOPES)
    assert [v["release_basis"] for v in vintages] == ["retrieval_time", "retrieval_time"]
    assert vintages[0]["published_at_ms"] < vintages[1]["published_at_ms"]


# ------------------------------------------------------------------- EN05


def test_ember_releases_are_vintages_and_shares_are_quoted_as_embers():
    conn = duckdb.connect()
    acquire(conn, "energy-ember-generation")
    later = acquire(conn, "ember_generation_release_2")
    assert later["applied"]["vintages"] == 3
    solar = next(s for s in _series(conn, provider="ember") if s["native_id"].endswith(":Solar"))
    vintages = EnergyStore(conn).vintages(NS, solar["series_id"], scopes=SCOPES)
    assert [v["record"]["release"]["label"] for v in vintages] == ["Ember monthly electricity data 2026-08",
                                                                   "Ember monthly electricity data 2026-09"]
    figures = vintages[0]["record"]["publisher_figures"]
    assert figures["publisher"] == "Ember" and "not computed by Noesis" in figures["note"]
    total = next(s for s in _series(conn, provider="ember") if s["native_id"].endswith("Total generation"))
    assert total["facets"]["aggregate_series"] is True
    assert "emissions" in es.PROVIDER_CONTRACTS["ember"]["quoted_only"]


# ------------------------------------------------------------------- EN06


def test_eurostat_balances_keep_flags_verbatim_and_vintage_by_last_update():
    conn = duckdb.connect()
    acquire(conn, "energy-eurostat-balances")
    steps = es.plan("eurostat", fb.EUROSTAT_SELECTION)
    assert steps[0]["url"].startswith("https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/nrg_bal_c/")
    fr = next(s for s in _series(conn, provider="eurostat") if s["native_id"] == "GIC:TOTAL:KTOE:FR")
    first = _latest(conn, fr)
    assert first["record"]["values"][1]["flags"]["OBS_FLAG"] == "ep" and first["status"] == "provisional"
    missing = next(s for s in _series(conn, provider="eurostat") if s["native_id"] == "NRGSUP:TOTAL:KTOE:FR")
    record = _latest(conn, missing)["record"]
    assert record["values"][1]["value"] is None and record["values"][0]["flags"]["OBS_FLAG"] == "b"
    acquire(conn, "eurostat_balances_later")
    de = next(s for s in _series(conn, provider="eurostat") if s["native_id"] == "GIC:TOTAL:KTOE:DE")
    vintages = EnergyStore(conn).vintages(NS, de["series_id"], scopes=SCOPES)
    assert [v["release_basis"] for v in vintages] == ["provider_last_update"] * 2
    assert [v["record"]["release"]["released_at"] for v in vintages] == ["2026-06-12T23:00:00Z", "2026-09-15T23:00:00Z"]
    assert [v["status"] for v in vintages] == ["provisional", "unknown"]


# ------------------------------------------------------------------- EN07


def test_energy_charts_follows_the_licence_decision_and_stays_separate_from_entsoe():
    conn = duckdb.connect()
    acquire(conn, "energy-entsoe")
    result = acquire(conn, "energy-charts-public-power")
    assert result["coverage"]["notes"]["energy_charts_skipped"][0]["name"] == "Residual load"
    charts = _series(conn, provider="energy-charts")
    assert {s["record_type"] for s in charts} == {"generation", "load"}
    record = _latest(conn, charts[0])["record"]
    assert record["derived_from"]["provider"] == "entsoe" and "never" not in record["derived_from"]["reconciliation"]
    assert record["licence"]["id"] == "energy-charts-cc-by-4.0" and "Energy-Charts" in record["attribution"]
    assert {s["series_id"] for s in charts}.isdisjoint({s["series_id"] for s in _series(conn, provider="entsoe")})
    with pytest.raises(ProviderError) as link_only:
        es.plan("energy-charts", {**fb.ENERGY_CHARTS_SELECTION, "endpoint": "price"})
    assert link_only.value.code == "link_only"
