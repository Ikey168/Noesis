"""Logistics series linked to trade flows and other Economics series by shared identifiers or citation (#2229,
SL08 #2544)."""

from __future__ import annotations

import pytest

from services.ingest.common.series_model import Observation, SeriesRecord
from src.domains.economic.model import register_series
from src.kb.logistics_ports import LogisticsPorts
from src.kb.logistics_records import LogisticsError
from src.kb.logistics_series import LogisticsStore
from src.kb.trade_flows import TradeFlowStore
from tests.unit import logistics_harness as h
from tests.unit import trade_harness as th


def _loaded(with_trade: bool = True):
    conn = h.connection()
    if with_trade:
        th.load_all(conn)
    for name in ("unlocode", "unctad", "eurostat"):
        h.apply(conn, name, retrieved_at_ms=h.FIRST_RETRIEVAL)
    return conn, LogisticsPorts(conn, now=lambda: h.FIRST_RETRIEVAL), LogisticsStore(conn)


def test_country_and_port_series_join_trade_flows_only_through_shared_published_codes():
    conn, ports, store = _loaded()
    result = ports.link_trade_flows(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert result["status"] == "linked"
    (germany_teu,) = store.find_series(h.NS, concept="container_port_throughput", codes=[("m49", "276")])
    links = ports.links(h.NS, series_id=germany_teu["series_id"])
    assert links and {link["basis"] for link in links} == {"shared-m49-code"}
    assert {link["shared"]["code"] for link in links} == {"276"}
    for link in links:
        target = link["target"]
        assert target["kind"] == "trade-series"
        assert link["side_by_side"]["combined"] is False
        assert link["side_by_side"]["logistics"]["unit"] == "TEU" and link["side_by_side"]["other"]["unit"]
    (germany_goods,) = store.find_series(h.NS, geo_kind="country", codes=[("eurostat-geo", "DE")])
    assert {link["basis"] for link in ports.links(h.NS, series_id=germany_goods["series_id"])} == {
        "shared-eurostat-geo-code"}
    # A port joins through the country code of its published UN/LOCODE (Hamburg -> DE), never by its name.
    (hamburg,) = store.find_series(h.NS, concept="port_calls", codes=[("unlocode", "DEHAM")])
    assert {link["basis"] for link in ports.links(h.NS, series_id=hamburg["series_id"])} == {"unlocode-country-code"}
    # An unmatched Eurostat port code (Bremerhaven, no accepted match yet) and the Netherlands have no trade link.
    (bremerhaven,) = store.find_series(h.NS, codes=[("eurostat-port", "DE003")])
    assert ports.trade_join(h.NS, bremerhaven["series_id"])["status"] == "none_on_record"
    (netherlands,) = store.find_series(h.NS, concept="container_port_throughput", codes=[("m49", "528")])
    assert ports.trade_join(h.NS, netherlands["series_id"]) == {
        "series_id": netherlands["series_id"], "status": "none_on_record", "links": []}
    unlinked = {u["series_id"] for u in result["unlinked"]}
    assert {bremerhaven["series_id"], netherlands["series_id"]} <= unlinked
    # Re-running adds no link.
    before = len(ports.links(h.NS))
    ports.link_trade_flows(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert len(ports.links(h.NS)) == before


def test_without_trade_records_the_join_reports_none_on_record():
    conn, ports, store = _loaded(with_trade=False)
    result = ports.link_trade_flows(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert result["status"] == "none_on_record" and result["linked"] == []
    with pytest.raises(LogisticsError) as missing:
        ports.link_by_citation(h.NS, store.find_series(h.NS)[0]["series_id"], "trade-series", "trade-series:x",
                               {"text": "cited", "locator": "p. 1"}, principal_id="svc", scopes=h.SCOPES)
    assert missing.value.code == "target_not_found"
    with pytest.raises(LogisticsError) as scoped:
        ports.link_trade_flows(h.NS, principal_id="svc", scopes={"knowledge:logistics:write", "namespace:global:write"})
    assert scoped.value.code == "unauthorized"


def test_other_economics_series_link_by_country_code_with_the_comparability_check_and_explicit_citations():
    conn, ports, store = _loaded()
    register_series(conn, SeriesRecord(
        series_id="worldbank:NE.EXP.GNFS.CD:DE", provider="worldbank", title="Exports of goods and services",
        frequency="annual", as_of=h.day_ms("2024-03-01"), observations=[Observation("2022", 1.0)], unit="usd",
        geography="DE", source_url="https://api.worldbank.org/v2/"), domain="economics")
    result = ports.link_economic_series(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert result["status"] == "linked"
    (germany_goods,) = store.find_series(h.NS, geo_kind="country", codes=[("eurostat-geo", "DE")])
    (link,) = ports.links(h.NS, series_id=germany_goods["series_id"], target_kind="economic-series")
    assert link["target"]["id"] == "worldbank:NE.EXP.GNFS.CD:DE" and link["basis"] == "shared-country-code"
    comparability = link["side_by_side"]["comparability"]
    assert comparability["comparable"] is False and {b["dimension"] for b in comparability["blockers"]} >= {"unit"}
    assert link["side_by_side"]["combined"] is False
    # An explicit citation joins a series to a trade series; the citation is stored with the link.
    trade_series = TradeFlowStore(conn, initialize=False).find_series("global", reporter_codes=["DE"])[0]
    (hamburg,) = store.find_series(h.NS, concept="port_calls", codes=[("unlocode", "DEHAM")])
    cited = ports.link_by_citation(
        h.NS, hamburg["series_id"], "trade-series", trade_series["series_id"],
        {"text": "Hamburg handles most of Germany's container trade with France", "locator": "report p. 4"},
        principal_id="analyst", scopes=h.SCOPES)
    assert cited["basis"] == "explicit-citation" and cited["created"] is True
    with pytest.raises(LogisticsError) as bad:
        ports.link_by_citation(h.NS, hamburg["series_id"], "trade-series", trade_series["series_id"],
                               {"text": ""}, principal_id="analyst", scopes=h.SCOPES)
    assert bad.value.code == "invalid_citation"
