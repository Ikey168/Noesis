"""Legislation monitors on subscriptions: actions, text versions, stages and votes (#2440)."""

from __future__ import annotations

import pytest

from src.ingestion.legislation_sources import FIXTURE_SECRET, fixture_transport
from src.kb.legislation import LegislationError, forbidden_keys
from src.kb.legislation_monitoring import LegislationMonitor
from src.kb.subscriptions import SubscriptionStore
from tests.unit import legislation_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    yield connection
    connection.close()


def run(monitor, subscription, watermark):
    SubscriptionStore(monitor.conn).commit_watermark(h.NS, watermark)
    return monitor.run(subscription["subscription_id"], principal_id="alice", scopes=h.SCOPES)


def test_new_revised_and_unchanged_bill_records(conn):
    monitor = LegislationMonitor(conn, now=h.Clock())
    watched = monitor.create(h.NS, "hr9901", watch="bill", key=h.US_BILL, principal_id="alice", scopes=h.SCOPES)
    first = run(monitor, watched, 1)["notifications"]
    kinds = sorted(n["kind"] for n in first)
    assert kinds.count("text_version_published") == 3 and kinds.count("vote_held") == 2
    assert kinds.count("bill_recorded") == 2  # congress.gov and BILLSTATUS, separately
    assert all(n["cites"]["revision_id"] and n["cites"]["previous_revision_id"] is None for n in first)
    assert run(monitor, watched, 2)["notifications"] == []  # unchanged
    h.apply(conn, "us-congress-gov-bills", v2=True)
    enr = [{"request": "/packages/BILLS-156hr9901enr/summary", "status": 200,
            "body": (h.FIXTURES / "govinfo_BILLS-156hr9901enr_summary.json").read_text()},
           {"request": "/packages/BILLS-156hr9901enr/xml", "status": 200,
            "body": (h.FIXTURES / "govinfo_BILLS-156hr9901enr.xml").read_text()}]
    item = h.source("us-govinfo-bills")
    item["legislation"]["selection"]["packages"].append("BILLS-156hr9901enr")
    item["budgets"]["max_pages"] = 4
    h.apply(conn, "us-govinfo-bills", item=item, extra_pages=enr)
    notes = run(monitor, watched, 3)["notifications"]
    by_kind = {n["kind"]: n for n in notes}
    actions = by_kind["actions_recorded"]
    assert [a["text"] for a in actions["actions"]] == [
        "Passed Senate without amendment by Yea-Nay Vote. 1 - 1. Record Vote Number: 55.",
        "Became Public Law No: 156-12."]
    assert actions["cites"]["previous_revision_id"] and actions["cites"]["previous_revision_id"] != \
        actions["cites"]["revision_id"]
    assert by_kind["law_cited"]["laws"] == ["Pub. L. 156-12"]
    assert by_kind["text_version_published"]["record_key"] == "us-text-version:BILLS-156hr9901enr"
    assert all(forbidden_keys(n) == [] and "predict" not in n["message"] for n in notes)


def test_uk_stages_royal_assent_and_corrected_divisions(conn):
    monitor = LegislationMonitor(conn, now=h.Clock())
    watched = monitor.create(h.NS, "uk", watch="bill", key=h.UK_BILL, principal_id="alice", scopes=h.SCOPES)
    first = run(monitor, watched, 1)["notifications"]
    candidate = next(n for n in first if n["kind"] == "vote_held" and "commons" in n["record_key"])
    assert "unlinked candidate" in candidate["message"]
    h.apply(conn, "uk-parliament-bills", v2=True)
    h.apply(conn, "uk-commons-divisions", v2=True)
    kinds = {n["kind"] for n in run(monitor, watched, 2)["notifications"]}
    assert {"royal_assent_recorded", "text_version_published", "division_corrected"} <= kinds


def test_sponsor_watch_follows_the_bills_a_member_sponsored(conn):
    monitor = LegislationMonitor(conn, now=h.Clock())
    watched = monitor.create(h.NS, "sponsor", watch="sponsor", key="legislation:member:us-bioguide:E009902",
                             principal_id="alice", scopes=h.SCOPES)
    notes = run(monitor, watched, 1)["notifications"]
    assert {n["bill_key"] for n in notes} == {h.US_BILL}
    with pytest.raises(LegislationError):
        monitor.create(h.NS, "bad", watch="bill", key="hr9901", principal_id="alice", scopes=h.SCOPES)


def test_live_revisions_from_unverified_sources_are_withheld(conn):
    conn.execute("UPDATE legislation_records SET evidence_origin='live' WHERE source_id='us-senate-roll-calls'")
    monitor = LegislationMonitor(conn, now=h.Clock())
    watched = monitor.create(h.NS, "live", watch="bill", key=h.US_BILL, principal_id="alice", scopes=h.SCOPES)
    result = run(monitor, watched, 1)
    assert result["withheld_unverified_live_revisions"] == 1
    assert all("senate" not in n["record_key"] for n in result["notifications"])


def test_refresh_is_bounded_idempotent_and_receipted(conn):
    monitor = LegislationMonitor(conn, now=h.Clock())
    item = h.source("uk-parliament-bills")
    transport = fixture_transport(h.native_pages("uk-parliament-bills"))
    again = monitor.refresh(item, run_id="run:refresh", principal_id="alice", scopes=h.SCOPES,
                            transport=transport, secret=FIXTURE_SECRET)
    assert again["units"] == 1 and again["complete"] and again["counts"] == {
        "new": 0, "revised": 0, "unchanged": 8, "older-observation": 0}
    (receipt,) = again["receipts"]
    assert receipt["receipt"]["requests"] and receipt["outcome"]["counts"]["unchanged"] == 8
    with pytest.raises(LegislationError):
        monitor.refresh(item, run_id="run:denied", principal_id="bob", scopes=h.READ_ONLY, transport=transport)
