"""Logistics monitors through subscriptions (#2229, SL10 #2547)."""

from __future__ import annotations

import pytest

from src.ingestion.logistics_sources import fixture_transport
from src.kb.logistics_monitoring import LogisticsMonitor
from src.kb.logistics_ports import LogisticsPorts
from src.kb.logistics_records import LogisticsError, forbidden_keys
from tests.unit import logistics_harness as h


def _monitor(conn, now):
    return LogisticsMonitor(conn, now=lambda: now)


def test_each_event_type_is_emitted_once_with_record_ids_and_citations_and_unchanged_releases_emit_nothing():
    conn = h.connection()
    h.load_all(conn)
    LogisticsPorts(conn).propose_matches(h.NS, principal_id="svc", scopes=h.SCOPES)
    monitor = _monitor(conn, h.FIRST_RETRIEVAL)
    with pytest.raises(LogisticsError):
        monitor.create(h.NS, "bad", watch={"vessels": ["x"]}, principal_id="alice", scopes=h.SCOPES)
    sub = monitor.create(h.NS, "hamburg", watch={"ports": ["DEHAM", "DEWVN"], "countries": ["m49:276"]},
                         principal_id="alice", scopes=h.SCOPES)
    first = monitor.run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    kinds = {n["kind"] for n in first["notifications"]}
    assert kinds == {"new_vintage", "series_break"}
    brk = next(n for n in first["notifications"] if n["kind"] == "series_break")
    assert brk["break_id"] and brk["period"] == "2098" and brk["citation"]["release_id"]
    assert forbidden_keys(first) == []
    # An unchanged re-acquisition emits nothing.
    h.load_all(conn)
    assert monitor.run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    # Later releases: a revised UNCTAD value, and a UN/LOCODE release changing Hamburg and removing Wilhelmshaven.
    for name in ("unctad", "unlocode"):
        h.apply(conn, name, revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    later = _monitor(conn, h.SECOND_RETRIEVAL).run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    by_kind = {}
    for n in later["notifications"]:
        by_kind.setdefault(n["kind"], []).append(n)
    assert set(by_kind) == {"revised_value", "port_code_change"}
    (revised,) = by_kind["revised_value"]
    assert revised["changed_values"] == [{"period": "2098", "before": {"value_text": "7900", "status": "reported"},
                                          "after": {"value_text": "7950", "status": "reported"}}]
    assert revised["vintage_id"] and revised["previous_vintage_id"] and revised["citation"]["published_on"] == \
        "2099-11-15"
    changes = {n["unlocode"]: n for n in by_kind["port_code_change"]}
    assert changes["DEWVN"]["state"] == "removed" and changes["DEWVN"]["rematches"][0]["trigger"] == "removed"
    assert changes["DEHAM"]["change_indicator"] == "|" and changes["DEHAM"]["release_version"] == "2099-2"
    # A restart replays the same watermark and emits nothing.
    again = _monitor(conn, h.SECOND_RETRIEVAL).run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert again["notifications"] == []
    polled = monitor.poll(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert polled


def test_new_months_of_a_freight_index_are_a_new_vintage_and_refresh_stays_within_budget():
    conn = h.connection()
    h.apply(conn, "bls", retrieved_at_ms=h.FIRST_RETRIEVAL)
    monitor = _monitor(conn, h.FIRST_RETRIEVAL)
    (series_id,) = [r[0] for r in conn.execute("SELECT series_id FROM logistics_series").fetchall()]
    sub = monitor.create(h.NS, "ppi", watch={"series": [series_id]}, principal_id="alice", scopes=h.SCOPES)
    assert {n["kind"] for n in monitor.run(sub["subscription_id"], principal_id="alice",
                                           scopes=h.SCOPES)["notifications"]} == {"new_vintage"}
    source = h.source("bls", revision=True)
    receipt = _monitor(conn, h.SECOND_RETRIEVAL).refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                                                         transport=fixture_transport(h.pages("bls", True)))
    assert receipt["status"] == "complete" and receipt["new_releases"] == 1
    later = _monitor(conn, h.SECOND_RETRIEVAL).run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    (notice,) = later["notifications"]
    assert notice["kind"] == "revised_value" and notice["added_periods"] == ["2099-09"]
    assert [c["period"] for c in notice["changed_values"]] == ["2099-08"]
    limited = [{"request": p["request"], "status": 429, "headers": {"Retry-After": "120"}, "body": ""}
               for p in h.pages("bls", True)]
    stopped = _monitor(conn, h.SECOND_RETRIEVAL + 1).refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                                                             transport=fixture_transport(limited))
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited"
    waiting = _monitor(conn, h.SECOND_RETRIEVAL + 2).refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                                                             transport=fixture_transport(limited))
    assert waiting["status"] == "rate_limited_wait"
