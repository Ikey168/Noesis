"""Port, country and route logistics series as of a vintage (#2229, SL09 #2546)."""

from __future__ import annotations

import pytest

from src.kb.logistics_ports import LogisticsPorts
from src.kb.logistics_queries import LogisticsQueries
from src.kb.logistics_records import LogisticsError, forbidden_keys
from tests.unit import logistics_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    ports = LogisticsPorts(conn, now=lambda: h.FIRST_RETRIEVAL)
    ports.import_crosswalk(h.NS, h.CROSSWALK, principal_id="operator-1", scopes=h.SCOPES)
    ports.propose_matches(h.NS, principal_id="svc", scopes=h.SCOPES)
    return conn, LogisticsQueries(conn)


def test_a_port_by_unlocode_returns_sources_side_by_side_with_the_vintage_known_at_the_date(env):
    _, queries = env
    before = queries.port(h.NS, "DEHAM", scopes=h.READ_ONLY, as_of_ms=h.day_ms("2024-08-01"), all_vintages=True)
    assert before["status"] == "answered" and forbidden_keys(before) == []
    by_provider = {e["provider"]: e for e in before["series"] if e["concept"] == "port_calls"}
    calls = by_provider["unctadstat"]
    assert calls["identity_basis"] == "embedded-unlocode"
    assert [v["value"] for v in calls["values"]] == ["7800", "7900"] and calls["later_vintages"] == 1
    assert len(calls["vintages"]) == 2 and calls["citation"]["published_on"] == "2024-07-15"
    goods = next(e for e in before["series"] if e["provider"] == "eurostat-maritime")
    assert goods["identity_basis"] == "published-crosswalk" and goods["concept"] == "goods_handled"
    assert set(before["side_by_side"]["by_concept"]) >= {"port_calls", "goods_handled",
                                                          "port_liner_shipping_connectivity"}
    after = queries.port(h.NS, "DEHAM", scopes=h.READ_ONLY, as_of_ms=h.day_ms("2024-12-31"))
    revised = next(e for e in after["series"] if e["concept"] == "port_calls")
    assert revised["values"][1]["value"] == "7950" and revised["vintage"]["revision_of"] == calls["vintage"]["vintage_id"]
    assert after["port"]["revision"] == 2 and before["port"]["revision"] == 1
    # Too early: nothing released yet.
    early = queries.port(h.NS, "DEHAM", scopes=h.READ_ONLY, as_of_ms=h.day_ms("2024-01-01"))
    assert early["status"] == "none_on_record" and {e["status"] for e in early["series"]} == {"not_yet_released"}


def test_value_history_lists_all_vintages_and_source_codes_and_unmatched_codes_answer_on_their_own(env):
    _, queries = env
    (calls,) = [e for e in queries.port(h.NS, "unctad-port:1101", scopes=h.READ_ONLY)["series"]
                if e["concept"] == "port_calls"]
    history = queries.value_history(h.NS, calls["series_id"], "2023", scopes=h.READ_ONLY)
    assert [x["value"]["value"] for x in history["history"]] == ["7900", "7950"]
    assert all(x["citation"]["file_sha256"] for x in history["history"])
    other = queries.port(h.NS, "eurostat-port:DE999", scopes=h.READ_ONLY)
    assert other["status"] == "answered" and other["identity"][0]["status"] == "unmatched"
    # Bremerhaven's Eurostat code is only a pending candidate, so DEBRV reaches no series through it.
    bremerhaven = queries.port(h.NS, "DEBRV", scopes=h.READ_ONLY)
    assert bremerhaven["status"] == "none_on_record"
    assert queries.port(h.NS, "NLAMS", scopes=h.READ_ONLY)["status"] == "none_on_record"
    with pytest.raises(LogisticsError):
        queries.port(h.NS, "DEHAM", scopes={"namespace:global:read"})


def test_countries_and_routes_are_answered_only_from_series_published_at_that_level(env):
    _, queries = env
    germany = queries.country(h.NS, "DE", scopes=h.READ_ONLY)
    assert [e["provider"] for e in germany["series"]] == ["eurostat-maritime"]
    assert "DEHAM" in germany["ports_on_record"]
    m49 = queries.country(h.NS, "m49:276", scopes=h.READ_ONLY)
    fleet = next(e for e in m49["series"] if e["concept"] == "merchant_fleet_by_flag")
    assert fleet["breaks"][0]["period"] == "2023"
    route = queries.route(h.NS, "DEHAM", "eurostat-port:NL002", scopes=h.READ_ONLY)
    assert route["status"] == "answered" and route["series"][0]["partner"]["code"] == "NL002"
    none = queries.route(h.NS, "DEHAM", "DEBRV", scopes=h.READ_ONLY)
    assert none["status"] == "none_on_record" and none["series"] == []


def test_a_revised_freight_index_and_the_excluded_indices_are_reported(env):
    _, queries = env
    first = queries.freight_indices(h.NS, scopes=h.READ_ONLY, as_of_ms=h.day_ms("2024-09-30"))
    (index,) = first["series"]
    assert [v["value"] for v in index["values"]] == ["110.4", "111.9", "112.5"]
    assert index["freight_index"]["decision"] == "in-scope" and index["licence"]["id"] == "us-public-domain"
    later = queries.freight_indices(h.NS, scopes=h.READ_ONLY)
    assert [v["value"] for v in later["series"][0]["values"]] == ["110.4", "111.9", "113.1", "114"]
    excluded = {i["index_id"]: i for i in later["excluded"]}
    assert excluded["baltic-dry-index"]["status"] == "excluded by licence decision"
    assert all(i["reason"] for i in excluded.values())
