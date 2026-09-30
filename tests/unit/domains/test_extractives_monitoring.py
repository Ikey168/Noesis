"""Extractives monitors through subscriptions (#2653, EX10 #2702)."""

from __future__ import annotations

import pytest

from src.ingestion.extractives_sources import fixture_transport
from src.kb.extractives_monitoring import ExtractivesMonitor
from src.kb.extractives_records import ExtractivesError, forbidden_keys
from tests.unit import extractives_harness as h


def _monitor(conn, now):
    return ExtractivesMonitor(conn, now=lambda: now)


def _kinds(result):
    out: dict[str, list] = {}
    for notice in result["notifications"]:
        out.setdefault(notice["kind"], []).append(notice)
    return out


def test_country_and_company_monitors_cite_new_revised_and_removed_records_and_stay_quiet_when_unchanged():
    conn = h.connection()
    h.reviewed(conn)
    monitor = _monitor(conn, h.FIRST_RETRIEVAL)
    with pytest.raises(ExtractivesError):
        monitor.create(h.NS, "bad", watch={"licences": ["x"]}, principal_id="alice", scopes=h.SCOPES)
    country = monitor.create(h.NS, "nl", watch={"countries": ["nl"]}, principal_id="alice", scopes=h.SCOPES)
    company = monitor.create(h.NS, "exampla-int", watch={"companies": [h.INT_ENTITY, h.NORTHWIND]},
                             principal_id="alice", scopes=h.SCOPES)
    first = _kinds(monitor.run(country["subscription_id"], principal_id="alice", scopes=h.SCOPES))
    assert set(first) == {"new_report"}
    (report,) = first["new_report"]
    assert report["report_key"] == h.NL_REPORT and report["summary"]["added"] == 15
    assert report["citation"]["release_version"] == "1" and report["citation"]["release_id"]
    payments = _kinds(monitor.run(company["subscription_id"], principal_id="alice", scopes=h.SCOPES))
    assert set(payments) == {"new_payment"}
    assert {n["record_key"] for n in payments["new_payment"]} >= {h.CIT_PAYMENT, h.NW_PAYMENT}
    # An unchanged re-acquisition emits nothing.
    h.load_all(conn)
    assert monitor.run(country["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    assert monitor.run(company["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    # The revised report version: one report notice and the changed and removed payments, with before and after.
    h.apply(conn, "eiti", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    later = _monitor(conn, h.SECOND_RETRIEVAL)
    revised = _kinds(later.run(country["subscription_id"], principal_id="alice", scopes=h.SCOPES))
    (notice,) = revised["report_revised"]
    assert notice["report_version"] == "2" and notice["summary"] == {"added": 0, "revised": 3, "removed": 1}
    assert {c["record_key"] for c in notice["changes"] if c["change"] == "removed"} == {h.NW_PAYMENT}
    changed = _kinds(later.run(company["subscription_id"], principal_id="alice", scopes=h.SCOPES))
    assert set(changed) == {"payment_revised", "payment_removed"}
    (payment,) = changed["payment_revised"]
    assert payment["record_key"] == h.CIT_PAYMENT and payment["previous_revision_id"] and payment["revision_id"]
    assert payment["before"]["company_reported"]["value"] == "1250000"
    assert payment["after"]["company_reported"]["value"] == "1200000"
    assert payment["citation"]["release_version"] == "2" and "record change" in payment["note"]
    (removed,) = changed["payment_removed"]
    assert removed["record_key"] == h.NW_PAYMENT and removed["after"] is None
    assert forbidden_keys(changed) == []
    # A restart replays the same watermark and emits nothing.
    again = _monitor(conn, h.SECOND_RETRIEVAL).run(country["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert again["notifications"] == []
    assert monitor.poll(country["subscription_id"], principal_id="alice", scopes=h.SCOPES)


def test_commodity_monitors_report_new_releases_and_revised_values_and_refresh_is_bounded_and_idempotent():
    conn = h.connection()
    h.apply(conn, "bgs", retrieved_at_ms=h.FIRST_RETRIEVAL)
    monitor = _monitor(conn, h.FIRST_RETRIEVAL)
    sub = monitor.create(h.NS, "copper-cl", watch={"commodities": ["copper"], "countries": ["CL"]},
                         principal_id="alice", scopes=h.SCOPES)
    first = _kinds(monitor.run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES))
    assert set(first) == {"new_commodity_release"}
    source = h.source("bgs", revision=True)
    later = _monitor(conn, h.SECOND_RETRIEVAL)
    receipt = later.refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("bgs", True)))
    assert receipt["status"] == "complete" and receipt["new_releases"] == 1 and receipt["receipt_id"]
    again = later.refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                          transport=fixture_transport(h.pages("bgs", True)))
    assert again["new_releases"] == 0 and again["unchanged_releases"] == 1  # idempotent
    notices = _kinds(later.run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES))
    (revised,) = notices["revised_values"]
    assert revised["changed_values"][0]["period"] == "2022" and revised["added_periods"] == ["2023"]
    assert revised["changed_values"][0]["before"]["value_text"] == "5300000"
    assert revised["citation"]["release_version"] == "2019-2023" and revised["vintage_id"]
    limited = [{"request": p["request"], "status": 429, "headers": {"Retry-After": "120"}, "body": ""}
               for p in h.pages("bgs", True)]
    stopped = _monitor(conn, h.SECOND_RETRIEVAL + 1).refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                                                             transport=fixture_transport(limited))
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited"
    waiting = _monitor(conn, h.SECOND_RETRIEVAL + 2).refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                                                             transport=fixture_transport(limited))
    assert waiting["status"] == "rate_limited_wait"
