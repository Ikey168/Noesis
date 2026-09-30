"""Fact-checks monitors: new, revised, withdrawn and unchanged records and publisher status changes (#2707, FC10)."""

from __future__ import annotations

import pytest

from src.kb.fact_checks_monitoring import FactChecksMonitor
from src.kb.fact_checks_records import FactCheckError
from src.kb.subscriptions import SubscriptionError, SubscriptionStore
from tests.unit import fact_checks_harness as h


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_publisher_monitor_reports_new_revised_withdrawn_and_status_changes_with_citations():
    conn = h.connection()
    h.load_all(conn)
    monitor = FactChecksMonitor(conn)
    watch = monitor.create(h.NS, "claimwatch", watch="publisher", key="https://www.claimwatch.example.com/",
                           principal_id="alice", scopes=h.SCOPES)
    assert "no new scheduler" in watch["refresh"]
    with pytest.raises(FactCheckError):
        monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)  # no committed watermark
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    first = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(first) == ["fact_check_published", "fact_check_published", "publisher_listed"]
    h.load_all(conn, version="v2", run_id="run:v2")
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    second = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(second) == ["fact_check_withdrawn", "publisher_status_changed"]
    status = next(n for n in second["notifications"] if n["kind"] == "publisher_status_changed")
    assert status["before"]["listing_status"] == "published" and status["after"]["listing_status"] == \
        "absent-from-listing"
    assert status["cites"]["previous_revision_id"] and status["cites"]["revision_id"] != \
        status["cites"]["previous_revision_id"]
    SubscriptionStore(conn).commit_watermark(h.NS, 3)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    assert monitor.poll(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["events"]


def test_topic_and_claimant_monitors_state_what_changed_and_claimants_need_the_scope():
    conn = h.connection()
    h.load_all(conn)
    monitor = FactChecksMonitor(conn)
    with pytest.raises(FactCheckError) as refused:
        monitor.create(h.NS, "mayor", watch="claimant", key="Mayor Alex Example", principal_id="alice",
                       scopes=h.SCOPES)
    assert refused.value.code == "unauthorized"
    claimant = monitor.create(h.NS, "mayor", watch="claimant", key="Mayor Alex Example", principal_id="alice",
                              scopes=h.REVIEW_SCOPES)
    topic = monitor.create(h.NS, "seals", watch="query", key="seals", principal_id="alice", scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    assert kinds(monitor.run(claimant["subscription_id"], principal_id="alice", scopes=h.REVIEW_SCOPES)) == \
        ["fact_check_published"] * 4
    assert len(monitor.run(topic["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]) == 4
    h.load_all(conn, version="v2", run_id="run:v2")
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    revised = monitor.run(topic["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(revised) == ["fact_check_revised", "fact_check_revised"]
    for note in revised["notifications"]:
        assert "claims" in note["changed"]
        assert note["before"]["claims"][0]["rating"]["textual_rating"] == "False"
        assert note["after"]["claims"][0]["rating"]["textual_rating"] == "Mostly false"
        assert note["cites"]["previous_revision_id"] and note["evidence_origin"] == "fixture"
        assert "verdict" not in note
    with pytest.raises((FactCheckError, SubscriptionError)):  # refused without the claimant scope
        monitor.run(claimant["subscription_id"], principal_id="alice", scopes=h.SCOPES | {
            "knowledge:subscriptions:write"})


def test_refresh_is_bounded_idempotent_and_leaves_receipts():
    conn = h.connection()
    monitor = FactChecksMonitor(conn)
    from src.ingestion.fact_checks_sources import FIXTURE_SECRET, fixture_transport

    source = h.source(h.GOOGLE)
    first = monitor.refresh(source, run_id="refresh-1", principal_id="alice", scopes=h.SCOPES,
                            transport=fixture_transport(h.native_pages(h.GOOGLE)), secret=FIXTURE_SECRET)
    assert first["complete"] and first["units"] == 2 and first["counts"]["new"] == 4
    assert len(first["receipts"]) == 2
    again = monitor.refresh(source, run_id="refresh-2", principal_id="alice", scopes=h.SCOPES,
                            transport=fixture_transport(h.native_pages(h.GOOGLE)), secret=FIXTURE_SECRET)
    assert again["counts"]["new"] == 0 and again["counts"]["unchanged"] == 4
    with pytest.raises(FactCheckError):
        monitor.refresh(source, run_id="refresh-3", principal_id="alice", scopes=h.READ_ONLY,
                        transport=fixture_transport(h.native_pages(h.GOOGLE)), secret=FIXTURE_SECRET)
