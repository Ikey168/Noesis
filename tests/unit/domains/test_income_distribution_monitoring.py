"""Income monitors through platform subscriptions: new, revised and unchanged cases (#2583, IP10)."""

from __future__ import annotations

import pytest

from src.ingestion.income_distribution_sources import fixture_transport
from src.kb.income_distribution_monitoring import IncomeMonitor, change_kinds
from src.kb.income_distribution_records import IncomeError
from tests.unit import income_distribution_harness as h

DEU = {"scheme": "iso3166-1-alpha3", "code": "DEU"}


def run(conn, subscription_id, at):
    return IncomeMonitor(conn, now=lambda: at).run(subscription_id, principal_id="alice", scopes=h.SCOPES)


def test_new_revised_and_unchanged_releases_notify_once_citing_the_records():
    conn = h.connection()
    h.load_all(conn)
    monitor = IncomeMonitor(conn, now=lambda: h.FIRST_RETRIEVAL + 1)
    watch = monitor.create(h.NS, "deu-pip", target={"place": DEU, "provider": "pip"}, principal_id="alice",
                           scopes=h.SCOPES)
    first = run(conn, watch["subscription_id"], h.FIRST_RETRIEVAL + 1)
    assert [n["kind"] for n in first["notifications"]] == ["new_period"] * 4
    assert run(conn, watch["subscription_id"], h.FIRST_RETRIEVAL + 2)["notifications"] == []  # unchanged
    h.load_all(conn, revisions=True)
    second = run(conn, watch["subscription_id"], h.SECOND_RETRIEVAL + 1)
    kinds = sorted(n["kind"] for n in second["notifications"])
    assert kinds == ["new_period"] * 4 + ["ppp_revision"] * 4
    ppp = next(n for n in second["notifications"] if n["kind"] == "ppp_revision")
    assert ppp["previous_vintage_id"] and ppp["vintage_id"] != ppp["previous_vintage_id"]
    assert ppp["source_revision"]["release_version"] == "20991120_2017_02_02_PROD" and ppp["detail"]["basis"]
    assert run(conn, watch["subscription_id"], h.SECOND_RETRIEVAL + 2)["notifications"] == []  # restart: nothing


def test_indicator_watch_hears_withdrawals_and_definition_changes():
    conn = h.connection()
    h.load_all(conn)
    watch = IncomeMonitor(conn, now=lambda: h.FIRST_RETRIEVAL + 1).create(
        h.NS, "gini", target={"concept": "gini_index"}, principal_id="alice", scopes=h.SCOPES)
    run(conn, watch["subscription_id"], h.FIRST_RETRIEVAL + 1)
    h.load_all(conn, revisions=True)
    kinds = {n["kind"] for n in run(conn, watch["subscription_id"], h.SECOND_RETRIEVAL + 1)["notifications"]}
    assert {"withdrawn", "definition_change", "revised_value", "ppp_revision"} <= kinds


def test_change_kinds_are_record_facts():
    assert change_kinds({"withdrawn": True, "removed_periods": ["2096"]}) == ["withdrawn"]
    assert change_kinds({"new_periods": [], "revised": [], "removed_periods": []}) == ["new_release"]
    with pytest.raises(IncomeError):
        IncomeMonitor(h.connection()).create(h.NS, "x", target={"forecast": True}, principal_id="a", scopes=h.SCOPES)


def test_refresh_is_bounded_idempotent_and_receipted():
    conn = h.connection()
    monitor = IncomeMonitor(conn, now=lambda: h.FIRST_RETRIEVAL)
    source = h.source("eusilc")
    transport = fixture_transport(h.pages("eusilc"))
    bounded = monitor.refresh(h.NS, source, principal_id="op", scopes=h.SCOPES, transport=transport, max_documents=2)
    assert bounded["status"] == "bounded" and bounded["new_releases"] == 2
    complete = monitor.refresh(h.NS, source, principal_id="op", scopes=h.SCOPES, transport=transport)
    assert complete["status"] == "complete" and complete["unchanged_releases"] == 2 and complete["new_releases"] == 2
    again = monitor.refresh(h.NS, source, principal_id="op", scopes=h.SCOPES, transport=transport)
    assert again["new_releases"] == 0 and again["unchanged_releases"] == 4
    limited = fixture_transport([{**p, "status": 429, "headers": {"Retry-After": "3600"}} for p in h.pages("eusilc")])
    stopped = monitor.refresh(h.NS, source, principal_id="op", scopes=h.SCOPES, transport=limited)
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited"
    waiting = monitor.refresh(h.NS, source, principal_id="op", scopes=h.SCOPES, transport=transport)
    assert waiting["status"] == "rate_limited_wait" and waiting["releases"] == []
    assert conn.execute("SELECT count(*) FROM income_refresh_receipts").fetchone()[0] == 5
