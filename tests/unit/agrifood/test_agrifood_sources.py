"""FAOSTAT, NASS Quick Stats, FAS PSD, Eurostat and Agri-food portal acquisition through the runtime (AF03-AF06)."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator

from src.ingestion.agrifood_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    SECRET_REFS,
    AgrifoodSourceAdapter,
    fixture_transport,
    psd_estimate_type,
    selection_entries,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError, validate_source_pack
from src.kb.agrifood_records import FLAG_VOCABULARIES
from tests.unit.agrifood import harness as h

NS = h.NS


@pytest.fixture()
def env():
    item = h.Env()
    yield item
    item.conn.close()


def _series(env, provider, **match):
    return [s for s in env.store.series_list(NS, provider=provider)
            if all(s[k] == v for k, v in match.items())]


def _values(env, series):
    return {v["period_key"]: v for vintage in env.store.vintages(NS, series["series_id"])
            for v in env.store.values(NS, vintage["vintage_id"])}


def test_pack_validates_pins_fixtures_and_records_access_decisions():
    raw = json.loads(h.PACK.read_text())
    schema = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-source-pack-v1.json").read_text())
    pack = validate_source_pack(raw)
    assert not list(Draft7Validator(schema).iter_errors(pack))
    result = SourcePackConformance(h.ROOT).offline(raw)
    assert result["valid"] and len(result["sources"]) == 5
    assert {s["agrifood"]["provider"] for s in pack["sources"]} == set(PROVIDER_CONTRACTS)
    for source in pack["sources"]:
        provider = source["agrifood"]["provider"]
        expected = ({"kind": "required-secret", "secret_ref": SECRET_REFS[provider]} if provider in SECRET_REFS
                    else {"kind": "none"})
        assert source["auth"] == expected
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert {"licence", "terms_url", "attribution", "rate_limits", "revision_behaviour", "authentication",
                "flags", "endpoints"} <= set(contract)
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    assert {"faostat-flags", "eurostat-obs-flags", "nass-value-codes"} <= set(FLAG_VOCABULARIES)
    assert "fixture-credential" not in json.dumps(pack)
    assert (h.ROOT / "docs/roadmaps/agrifood-source-audit.md").is_file()


def test_selections_are_explicit_and_checked():
    source = copy.deepcopy(next(s for s in h.manifest()["sources"] if s["agrifood"]["provider"] == "faostat"))
    provider, entries = selection_entries(source)
    assert provider == "faostat" and len(entries) == 5
    bad = copy.deepcopy(source)
    bad["agrifood"]["selection"][0]["domain"] = "TCL"
    with pytest.raises(SourcePackError) as caught:
        selection_entries(bad)
    assert caught.value.code == "invalid_manifest"
    moved = copy.deepcopy(source)
    moved["endpoint"] = "https://example.org/api"
    with pytest.raises(SourcePackError):
        selection_entries(moved)
    unbounded = copy.deepcopy(source)
    unbounded["agrifood"]["selection"][0]["years"] = list(range(2000, 2020))
    with pytest.raises(SourcePackError):
        selection_entries(unbounded)


def test_faostat_flags_notes_food_balances_and_domain_vintages(env):
    assert env.run(source_ids=["faostat-qcl-pp-fbs"])["status"] == "complete"
    outcomes = env.store.runs(NS)[0]["outcomes"]
    assert [o["outcome"] for o in outcomes].count("no_data") == 1  # Germany soya beans
    (production,) = _series(env, "faostat", commodity_code="56", place_code="231", element_code="5510")
    assert production["dataset"] == "QCL" and production["unit"] == "t" and production["period_type"] == "calendar-year"
    values = _values(env, production)
    assert values["2023"]["flag"] == {"vocabulary": "faostat-flags", "code": "A", "label": "Official figure",
                                      "classes": ["official"], "note": None}
    (yield_series,) = _series(env, "faostat", element_code="5412")
    assert _values(env, yield_series)["2022"]["flag"]["classes"] == ["estimated"]
    (france,) = _series(env, "faostat", place_code="68")
    imputed = _values(env, france)["2023"]
    assert imputed["flag"]["code"] == "I" and imputed["flag"]["note"] == "Imputed from the national crop survey total"
    (price,) = _series(env, "faostat", dataset="PP")
    missing = _values(env, price)["2023"]
    assert missing["status"] == "missing" and missing["value"] is None and missing["flag"]["code"] == "O"
    fbs = _series(env, "faostat", dataset="FBS")
    assert {s["record_type"] for s in fbs} == {"food_balance"} and len(fbs) == 2
    (vintage,) = env.store.vintages(NS, production["series_id"])
    assert vintage["released_at"] == "2024-12-18" and "date_update" in vintage["release_basis"]
    assert {r["release_key"] for r in env.store.releases(NS, provider="faostat")} >= {"faostat:QCL:2024-12-18"}
    # A domain update is a new vintage; the prior one stays queryable.
    assert env.run("agrifood-2", source_ids=["faostat-qcl-pp-fbs"], overrides=h.LATER)["status"] == "complete"
    vintages = env.store.vintages(NS, production["series_id"])
    assert [v["released_at"] for v in vintages] == ["2024-12-18", "2025-03-20"]
    by_release = [env.store.values(NS, v["vintage_id"])[1]["value"] for v in vintages]
    assert by_release == ["389694460", "389667000"]


def test_nass_geographies_withheld_forecast_programs_and_load_time_vintages(env):
    assert env.run(source_ids=["nass-quickstats-crops"])["status"] == "complete"
    outcomes = env.store.runs(NS)[0]["outcomes"]
    assert [o["outcome"] for o in outcomes].count("no_data") == 1  # Iowa wheat
    (county,) = _series(env, "nass-quickstats", place_code="19169")
    assert county["place_scheme"] == "us-fips" and county["place_level"] == "county"
    withheld = _values(env, county)["2022"]
    assert withheld["status"] == "withheld" and withheld["value"] is None and withheld["value_text"] == "(D)"
    assert withheld["flag"]["classes"] == ["withheld"]
    (iowa_yield,) = _series(env, "nass-quickstats", place_code="19", measure_kind="yield")
    values = _values(env, iowa_yield)
    assert values["2023 YEAR - AUG FORECAST"]["estimate_type"] == "publisher-forecast"
    assert values["2023"]["estimate_type"] == "observation" and values["2023"]["value"] == "201"
    (price,) = _series(env, "nass-quickstats", measure_kind="producer_price")
    assert price["period_type"] == "marketing-year" and price["place_scheme"] == "iso3166-1"
    soy = _series(env, "nass-quickstats", commodity_code="SOYBEANS")
    assert {s["program"] for s in soy} == {"SURVEY", "CENSUS"}  # never merged
    (national,) = _series(env, "nass-quickstats", place_code="US", measure_kind="production")
    assert [v["released_at"] for v in env.store.vintages(NS, national["series_id"])] == [
        "2023-01-12T12:00:00", "2024-01-12T12:00:00"] or [v["released_at"] for v in env.store.vintages(
            NS, national["series_id"])] == ["2023-01-12 12:00:00", "2024-01-12 12:00:00"]
    stored = json.dumps([v["statement"] for v in _values(env, national).values()])
    assert "fixture-credential" not in stored and "key=" not in stored
    assert env.run("agrifood-2", source_ids=["nass-quickstats-crops"], overrides=h.LATER)["status"] == "complete"
    vintages = env.store.vintages(NS, national["series_id"])
    assert len(vintages) == 3
    revised = [v for v in vintages if v["released_at"].startswith("2024-09-30")]
    assert env.store.values(NS, revised[0]["vintage_id"])[0]["value"] == "15341000000"


def test_nass_needs_its_secret():
    source = next(s for s in h.manifest()["sources"] if s["agrifood"]["provider"] == "nass-quickstats")
    adapter = AgrifoodSourceAdapter(source, transport=fixture_transport(h.pages("nass-quickstats-crops")))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "series"}, cursor=None)
    assert caught.value.code == "authentication_failed"


def test_psd_marketing_years_projections_and_monthly_releases(env):
    assert env.run(source_ids=["fas-psd-balances"])["status"] == "complete"
    series = _series(env, "fas-psd", commodity_code="0440000", place_code="US")
    assert {s["element"] for s in series} == {"Production", "Exports", "Ending Stocks"}  # Area Harvested not selected
    assert {s["period_type"] for s in series} == {"marketing-year"} and {s["record_type"] for s in series} == {
        "food_balance"}
    (production,) = [s for s in series if s["element"] == "Production"]
    values = _values(env, production)
    assert values["2025"]["estimate_type"] == "publisher-projection" and values["2025"]["flag"]["note"] == \
        "USDA projection"
    assert values["2024"]["estimate_type"] == "publisher-estimate"
    assert "never converted" in values["2025"]["period"]["definition"]
    assert production["unit"] == "(1000 MT)"
    assert psd_estimate_type(2025, 2026, 2) == ("publisher-projection", "projection")
    assert psd_estimate_type(2022, 2025, 9) == ("observation", "not-flagged")
    assert env.run("agrifood-2", source_ids=["fas-psd-balances"], overrides=h.LATER)["status"] == "complete"
    vintages = env.store.vintages(NS, production["series_id"])
    assert [v["released_at"] for v in vintages] == ["2025-09", "2025-10"]
    october = {v["period_key"]: v["value"] for v in env.store.values(NS, vintages[1]["vintage_id"])}
    assert october["2025"] == "427000"


def test_eurostat_through_the_sdmx_connector_with_flags_and_last_update(env):
    assert env.run(source_ids=["eurostat-agri-crops-prices"])["status"] == "complete"
    (fr_yield,) = _series(env, "eurostat-agri", place_code="FR", measure_kind="yield")
    combined = _values(env, fr_yield)["2023"]
    assert combined["flag"]["code"] == "ep" and combined["flag"]["classes"] == ["estimated", "provisional"]
    (de_yield,) = _series(env, "eurostat-agri", place_code="DE", measure_kind="yield")
    confidential = _values(env, de_yield)["2023"]
    assert confidential["status"] == "withheld" and confidential["value"] is None and confidential["value_text"] == ":"
    (vintage,) = env.store.vintages(NS, fr_yield["series_id"])
    assert vintage["released_at"] == "2025-07-15T23:00:00" and "LAST UPDATE" in vintage["release_basis"]
    assert fr_yield["unit"] == "Tonnes per hectare" and fr_yield["commodity_scheme"] == "eurostat-crops"
    (price,) = _series(env, "eurostat-agri", dataset="apri_ap_crpouta")
    assert price["measure_kind"] == "producer_price" and _values(env, price)["2023"]["flag"]["code"] == "p"


def test_portal_prices_keep_text_market_week_and_first_retrieval_vintage(env):
    assert env.run(source_ids=["agri-food-portal-cereal-prices"])["status"] == "complete"
    (wheat,) = _series(env, "agri-food-portal", commodity_code="BLTPAN")
    assert wheat["market"] == "Rouen - Delivered port" and wheat["period_type"] == "week" and wheat["unit"] == "€/t"
    week = _values(env, wheat)["2024-W01"]
    assert week["value_text"] == "€226.00" and week["value"] == "226"
    assert week["period"]["start"] == "2024-01-01" and week["period"]["end"] == "2024-01-07"
    (vintage,) = env.store.vintages(NS, wheat["series_id"])
    assert vintage["released_at"] is None and "first retrieval" in vintage["release_basis"]
    assert env.run("agrifood-2", source_ids=["agri-food-portal-cereal-prices"])["status"] == "complete"
    assert len(env.store.vintages(NS, wheat["series_id"])) == 1  # an unchanged answer is no new vintage


def test_replay_adds_nothing_and_receipts_mark_fixture_evidence(env):
    env.loaded()
    before = env.conn.execute("SELECT count(*) FROM agrifood_values").fetchone()[0]
    assert env.run("agrifood-replay")["status"] == "complete"
    assert env.conn.execute("SELECT count(*) FROM agrifood_values").fetchone()[0] == before
    assert {r["evidence_origin"] for r in env.store.runs(NS)} == {"fixture"}
    assert not h.forbidden_keys([v["statement"] for s in env.store.series_list(NS) for v in _values(env, s).values()])
