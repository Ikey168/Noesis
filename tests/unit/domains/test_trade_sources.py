"""Trade sources: audit contracts, Comtrade, Comext through the Eurostat connector and WITS concordances (#2535,
#2539, #2541, #2543)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
from src.ingestion.connectors.dataset.eurostat import EurostatConnector
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.ingestion.trade_sources import (
    BOUNDED_COVERAGE,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    TradeFlowsAdapter,
    fixture_transport,
    trade_declaration,
)
from tests.unit import trade_harness as h


def test_every_provider_has_a_recorded_contract_and_live_status():
    assert set(PROVIDER_CONTRACTS) == {"un-comtrade", "eurostat-comext", "wits", "unsd-classifications"}
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert contract["access_decision"] in {"unverified-live", "operator-import"}
        assert LIVE_VERIFICATION[provider]["status"] == contract["access_decision"]
        assert provider in BOUNDED_COVERAGE
    comtrade = PROVIDER_CONTRACTS["un-comtrade"]
    for key in ("authentication", "rate_limits", "terms", "revision_model", "classification_vintages"):
        assert comtrade[key]
    assert "NOESIS_COMTRADE_KEY" in comtrade["authentication"]
    audit = (h.ROOT / "docs/development/trade-evidence/source-audit.md").read_text()
    for name in ("UN Comtrade", "Comext", "WITS", "LIVE_VERIFICATION", "eurostat.py", "Bounded coverage"):
        assert name in audit


def test_the_source_pack_declares_the_sources_with_pinned_fixtures_that_replay_offline():
    manifest = h.manifest()
    sources = {s["source_id"]: s for s in manifest["sources"] if s["connector"] == "trade-flows"}
    assert set(sources) == set(h.SOURCES.values())
    assert sources["un-comtrade-trade-flows"]["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_COMTRADE_KEY"}
    assert all(s["trade_flows"]["live_verification"] == "unverified-live" for s in sources.values())
    report = SourcePackConformance(h.ROOT).offline({**manifest, "sources": list(sources.values())})
    assert report["valid"] and report["coverage"] == {"configured": 3, "verified": 3}


def test_comtrade_reporter_and_mirror_reports_are_acquired_as_separate_documents_with_release_stamps():
    first, second = h.fetch("comtrade")
    header = first[0]["trade_release"]
    assert header["published_at"].startswith("2099-05-15") and header["release_basis"] == "comtrade_last_released"
    assert header["structure"]["availability"][0]["first_released"].startswith("2098-04-01")
    assert header["evidence_origin"] == "fixture" and header["live_verification"] == "unverified-live"
    assert {r["trade_item"]["role"] for r in first} == {"reporter"}
    assert {r["trade_item"]["role"] for r in second} == {"mirror"}
    assert {r["trade_item"]["reporter"]["code"] for r in second} == {"156"}
    estimated = next(
        o for r in first for o in r["trade_item"]["observations"] if o["flags"].get("isQtyEstimated") is True
    )
    assert estimated["quantities"][0]["estimated"] is True and estimated["value"] == "380000"


def test_comtrade_needs_its_key_refuses_other_hosts_and_reports_rate_limits():
    source = h.source("comtrade")
    with pytest.raises(SourcePackError) as caught:
        TradeFlowsAdapter(source, transport=fixture_transport(h.pages("comtrade"))).fetch_page(
            {"operation": "release", "parameters": {}}, cursor=None
        )
    assert caught.value.code == "authentication_failed"
    seen = {}

    def limited(*, url, params, headers, timeout):
        seen.update(headers)
        return {"status": 429, "headers": {"Retry-After": "60"}, "content": b"", "origin": "fixture"}

    with pytest.raises(SourcePackError) as caught:
        TradeFlowsAdapter(source, transport=limited, secret="k").fetch_page(
            {"operation": "release", "parameters": {}}, cursor=None
        )
    assert caught.value.code == "rate_limited" and caught.value.details["retry_after_ms"] == 60000
    assert seen["Ocp-Apim-Subscription-Key"] == "k"

    def moved(*, url, params, headers, timeout):
        return {"status": 200, "content": b"{}", "final_url": "https://mirror.example.org/x"}

    with pytest.raises(SourcePackError) as caught:
        TradeFlowsAdapter(source, transport=moved, secret="k").fetch_page(
            {"operation": "release", "parameters": {}}, cursor=None
        )
    assert caught.value.code == "network_policy"
    with pytest.raises(SourcePackError):
        trade_declaration({**source, "endpoint": "https://example.org/data/v1"})
    wrong_role = json.loads(json.dumps(source))
    wrong_role["trade_flows"]["documents"][1]["role"] = "reporter"
    with pytest.raises(SourcePackError):
        trade_declaration(wrong_role)


def test_comtrade_results_over_the_budget_are_refused_not_truncated():
    source = h.source("comtrade")
    adapter = TradeFlowsAdapter(source, transport=fixture_transport(h.pages("comtrade")), secret="k")
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "release", "parameters": {}, "limit": 2}, cursor=None)
    assert caught.value.code == "budget_exhausted"


def test_the_eurostat_connector_reads_every_comext_cell_without_collapsing_or_zero_filling():
    pages = h.pages("comext")
    connector = EurostatConnector()
    raw = RawSeries(SeriesRef(locator="x", metadata={}), pages[0]["body"], content_type="application/json",
                    source_url="https://ec.europa.eu/x")
    cube = connector.parse_cells(raw)
    assert cube["cell_count"] == 16 and cube["absent_cells"] == 1 and len(cube["cells"]) == 15
    confidential = [c for c in cube["cells"] if c["status"] == "c"]
    assert len(confidential) == 2 and all(c["value"] is None for c in confidential)
    assert cube["updated_at_ms"] is not None
    url = connector.comext_url("DS-045409", "DE", {"partner": ["FR"], "flow": ["1", "2"], "freq": "M"})
    assert "flow=1&flow=2" in url and url.startswith("https://ec.europa.eu/eurostat/api/comext/")
    # The scalar statistics path encodes exactly as before.
    assert connector._url("demo_pjan", "DE", {"age": "TOTAL"}) == (
        "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/demo_pjan?format=JSON&geo=DE&age=TOTAL"
    )
    with pytest.raises(ValueError):
        connector.parse_cells(raw, max_cells=10)


def test_comext_flows_keep_cn_vintage_confidential_cells_and_release_by_cube_update():
    first, second = h.fetch("comext")
    header = first[0]["trade_release"]
    assert header["release_basis"] == "eurostat_dataset_updated" and header["published_on"] == "2099-03-15"
    assert header["structure"]["absent_cells"] == 1
    items = {(r["trade_item"]["flow"]["direction"], r["trade_item"]["product"]["code"]): r["trade_item"] for r in first}
    imported = items[("import", "29309098")]
    assert imported["classification"] == {"scheme": "CN", "vintage": "CN2099", "code": "CN"}
    feb = imported["observations"][1]
    assert feb["status"] == "confidential" and feb["value"] is None and feb["quantities"][0]["confidential"] is True
    exported = items[("export", "29309098")]
    jan = exported["observations"][0]
    assert jan["value"] == "310000" and jan["quantities"] == []  # the absent quantity is not invented
    assert {r["trade_item"]["role"] for r in second} == {"mirror"}
    assert items[("export", "85414300")]["unit"] == {"currency": "EUR", "scale": "1"}


def test_comext_revisions_are_new_vintages_and_earlier_vintages_remain_queryable():
    from src.kb.trade_flows import TradeFlowStore

    conn = h.connection()
    h.apply(conn, "comext", retrieved_at_ms=h.FIRST_RETRIEVAL)
    results = h.apply(conn, "comext", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert [r["status"] for r in results] == ["applied", "unchanged"]
    store = TradeFlowStore(conn)
    (series,) = store.find_series(h.NS, reporter_codes=["DE"], product_codes=["85414300"], flow_direction="export")
    old, new = store.vintage_rows(h.NS, series["series_id"])
    assert store.observations(h.NS, old["vintage_id"])[0]["value"] == "2000000"
    assert store.observations(h.NS, new["vintage_id"])[0]["value"] == "2080000"


def test_wits_concordances_are_read_from_the_zip_with_declared_columns():
    tables = [page[0]["trade_item"]["concordance"] for page in h.fetch("wits")]
    hs = next(t for t in tables if t["target"]["vintage"] == "HS2017")
    assert hs["member"].endswith(".CSV") and len(hs["rows"]) == 5
    row = next(r for r in hs["rows"] if r["source_code"] == "854143")
    assert row["mapping_type"] == "n:1" and row["source_label"].startswith("Photovoltaic")
    source = h.source("wits")
    broken = json.loads(json.dumps(source))
    broken["trade_flows"]["documents"][0]["columns"]["target_code"] = "HS 2017 Code"
    with pytest.raises(SourcePackError) as caught:
        TradeFlowsAdapter(broken, transport=fixture_transport(h.pages("wits"))).fetch_page(
            {"operation": "release", "parameters": {}}, cursor=None
        )
    assert caught.value.code == "schema_drift"
