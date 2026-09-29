"""Public-finance monitors on subscriptions: supplementary budgets, outturn vintages, payments, findings (#1987)."""

from __future__ import annotations

import pytest

from src.ingestion.public_finance_sources import PublicFinanceAdapter, fixture_transport
from src.kb.public_finance import (
    PublicFinanceError,
    PublicFinanceProjector,
    forbidden_keys,
)
from src.kb.public_finance_monitoring import PublicFinanceMonitor
from src.kb.subscriptions import SubscriptionStore
from tests.unit import public_finance_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    yield connection
    connection.close()


def run(monitor, subscription, watermark):
    SubscriptionStore(monitor.conn).commit_watermark("global", watermark)
    return monitor.run(
        subscription["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )


def test_a_watched_line_reports_each_publication_with_what_it_supersedes(conn):
    h.apply(conn, "bund", 0, h.BUND_FILES[0])
    monitor = PublicFinanceMonitor(conn)
    line = h.line_id(conn, "de-bund-haushalt", titel="68101")
    watch = monitor.create(
        "global",
        "grant",
        watch="budget_line",
        key=line,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert "no new scheduler" in watch["refresh"]
    assert [n["kind"] for n in run(monitor, watch, 1)["notifications"]] == [
        "plan_revision"
    ]
    h.apply(conn, "bund", 1, h.BUND_FILES[1])
    (supplementary,) = run(monitor, watch, 2)["notifications"]
    assert (
        supplementary["kind"] == "supplementary_plan"
        and supplementary["record_kind"] == "supplementary_plan"
    )
    assert supplementary["source_revision"]["document"] == "1. Nachtragshaushalt (Soll)"
    h.apply(conn, "bund", 2, h.BUND_FILES[2])
    h.apply(conn, "bund", 3, h.BUND_FILES[3])
    vintages = run(monitor, watch, 3)["notifications"]
    assert [n["kind"] for n in vintages] == ["outturn_vintage", "outturn_vintage"]
    first, final = sorted(vintages, key=lambda n: n["source_revision"]["published_on"])
    assert first["supersedes"] is None
    assert final["supersedes"]["release_id"] == first["source_revision"]["release_id"]
    h.load_findings(conn)
    findings = run(monitor, watch, 4)["notifications"]
    assert {n["kind"] for n in findings} == {"audit_finding"} and len(findings) == 2
    for note in [supplementary, *vintages, *findings]:
        assert forbidden_keys(note) == [] and "nothing is concluded" in note["note"]


def test_replays_and_restarts_emit_no_duplicate_events(conn):
    h.load_budgets(conn)
    monitor = PublicFinanceMonitor(conn)
    line = h.line_id(conn, "de-bund-haushalt", titel="53201")
    watch = monitor.create(
        "global",
        "study",
        watch="budget_line",
        key=line,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    first = run(monitor, watch, 1)["notifications"]
    assert (
        len(first) == 4
    )  # plan, supplementary plan and both outturn vintages (the second repeats the figure)
    for index, name in enumerate(h.BUND_FILES):
        h.apply(conn, "bund", index, name, run_id=f"replay:{index}")
    restarted = PublicFinanceMonitor(conn)
    assert run(restarted, watch, 2)["notifications"] == []
    assert (
        restarted.run(
            watch["subscription_id"], 2, principal_id="alice", scopes=h.SCOPES
        )["notifications"]
        == []
    )


def test_programmes_and_beneficiaries_report_payment_publications_and_revisions(conn):
    h.apply(conn, "fts", 0, h.FTS)
    monitor = PublicFinanceMonitor(conn)
    programme = monitor.create(
        "global",
        "horizon",
        watch="programme",
        key="Fictional Horizon Programme",
        principal_id="alice",
        scopes=h.SCOPES,
    )
    beneficiary = monitor.create(
        "global",
        "pt",
        watch="beneficiary",
        key="public-finance:beneficiary:eu-fts:vat:PT:PT999999990",
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert len(run(monitor, programme, 1)["notifications"]) == 3
    assert len(run(monitor, beneficiary, 1)["notifications"]) == 1
    republished = h.body(h.FTS).replace('"480,500.00"', '"481,000.00"')
    h.apply(
        conn,
        "fts",
        0,
        republished,
        headers={"Last-Modified": "Fri, 30 Jul 2100 10:00:00 GMT"},
    )
    (revised,) = run(monitor, beneficiary, 2)["notifications"]
    assert (
        revised["kind"] == "payment_publication" and revised["supersedes"]["payment_id"]
    )
    assert revised["item"]["amount_text"] == "481,000.00"
    assert [
        n["item"]["amount_text"] for n in run(monitor, programme, 2)["notifications"]
    ] == ["481,000.00"]


def test_live_releases_of_unverified_providers_are_withheld(conn):
    item = h.source("bund")
    natives = [
        {
            "request": "/static/daten/2025/soll/hh_2025_soll.csv",
            "status": 200,
            "headers": {},
            "body": h.body(h.BUND_FILES[0]),
        }
    ]
    replay = fixture_transport(natives)

    def live(**kwargs):
        response = dict(replay(**kwargs))
        response.pop("origin")
        return response

    page = PublicFinanceAdapter(item, transport=live).fetch_page(
        {"operation": "release", "parameters": {}, "limit": 100}, cursor=None
    )
    PublicFinanceProjector(conn).project_page(
        run_id="live",
        manifest=None,
        source=item,
        records=page.records,
        documents=[],
        page_receipt=page.receipt,
        principal_id="operator",
    )
    monitor = PublicFinanceMonitor(conn)
    line = h.line_id(conn, "de-bund-haushalt", titel="68101")
    watch = monitor.create(
        "global",
        "live",
        watch="budget_line",
        key=line,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    result = run(monitor, watch, 1)
    assert (
        result["notifications"] == [] and result["withheld_unverified_live_items"] == 1
    )


def test_monitors_need_a_known_watch_and_read_access(conn):
    h.apply(conn, "bund", 0, h.BUND_FILES[0])
    monitor = PublicFinanceMonitor(conn)
    with pytest.raises(PublicFinanceError):
        monitor.create(
            "global",
            "x",
            watch="ministry",
            key="98",
            principal_id="alice",
            scopes=h.SCOPES,
        )
    with pytest.raises(PublicFinanceError):
        monitor.create(
            "global",
            "x",
            watch="budget_line",
            key="pf-line:none",
            principal_id="alice",
            scopes=h.SCOPES,
        )
    with pytest.raises(PublicFinanceError):
        monitor.create(
            "global",
            "x",
            watch="programme",
            key="p",
            principal_id="alice",
            scopes={"knowledge:read"},
        )
