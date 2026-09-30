"""Fact-checks monitors: new, revised and unchanged fact-checks and publisher status changes (#2707)."""

from __future__ import annotations

import pytest

from src.ingestion.fact_checks_sources import FIXTURE_SECRET, fixture_transport
from src.kb.fact_checks_monitoring import FactCheckMonitor
from src.kb.fact_checks_records import FactCheckError
from src.kb.subscriptions import SubscriptionStore
from tests.unit import fact_checks_harness as h


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_publisher_monitor_reports_new_revised_and_unchanged_records_with_citations():
    conn = h.connection()
    h.load_all(conn)
    monitor = FactCheckMonitor(conn)
    watch = monitor.create(h.NS, "factdesk", watch="publisher", key="https://www.factdesk.example/",
                           principal_id="alice", scopes=h.SCOPES)
    assert "no new scheduler" in watch["refresh"] and watch["query"]["key"] == "factdesk.example"
    with pytest.raises(FactCheckError):
        monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)  # no committed watermark
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    first = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(first) == ["fact_check_published", "fact_check_published", "publisher_listed"]
    h.load_all(conn, version="v2")
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    second = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(second) == ["fact_check_published", "rating_changed", "review_date_changed"]
    rating = next(n for n in second["notifications"] if n["kind"] == "rating_changed")
    assert rating["before"]["text"] == "False"
    assert rating["after"]["text"] == "False (updated with the company's statement)"
    assert rating["cites"]["previous_revision_id"] and rating["cites"]["revision_id"] != \
        rating["cites"]["previous_revision_id"]
    SubscriptionStore(conn).commit_watermark(h.NS, 3)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    assert monitor.poll(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["events"]


def test_publisher_status_changes_and_absences_are_notified():
    conn = h.connection()
    h.load_all(conn)
    monitor = FactCheckMonitor(conn)
    northwind = monitor.create(h.NS, "nv", watch="publisher", key="northwind-verify.example", principal_id="alice",
                               scopes=h.SCOPES)
    contoso = monitor.create(h.NS, "cc", watch="publisher", key="contoso-check.example", principal_id="alice",
                             scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    monitor.run(northwind["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    monitor.run(contoso["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    h.load_all(conn, version="v2")
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    changed = monitor.run(northwind["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    (status,) = changed["notifications"]
    assert status["kind"] == "publisher_status_changed"
    assert (status["before"], status["after"]) == ("Verified signatory", "Expired")
    gone = monitor.run(contoso["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(gone) == ["fact_check_absent_from_source", "publisher_absent_from_listing"]


def test_claimant_and_topic_monitors_follow_accepted_matches_and_published_text():
    conn = h.accepted_world(v2=False)
    monitor = FactCheckMonitor(conn)
    by_entity = monitor.create(h.NS, "robin", watch="claimant", key="ent-wikidata-q99999901", principal_id="alice",
                               scopes=h.SCOPES)
    by_name = monitor.create(h.NS, "robin-name", watch="claimant", key="Robin Sample", principal_id="alice",
                             scopes=h.SCOPES)
    topic = monitor.create(h.NS, "fares", watch="query", key="bus fares", principal_id="alice", scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    assert len(monitor.run(by_entity["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]) == 1
    assert len(monitor.run(by_name["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]) == 2
    fares = monitor.run(topic["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(fares) == ["fact_check_published"]
    h.load_all(conn, version="v2")
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    later = monitor.run(topic["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    (new,) = later["notifications"]
    assert new["kind"] == "fact_check_published" and new["rating_as_published"]["text"] == "Misleading"
    assert "verdict" not in repr(later).casefold()


def test_refresh_is_bounded_idempotent_and_leaves_receipts():
    conn = h.connection()
    monitor = FactCheckMonitor(conn)
    source = h.source(h.GOOGLE)
    first = monitor.refresh(source, run_id="r1", principal_id="alice", scopes=h.SCOPES,
                            transport=fixture_transport(h.native_pages(h.GOOGLE)), secret=FIXTURE_SECRET)
    assert first["complete"] and first["units"] == 2 and first["counts"]["new"] == 4
    again = monitor.refresh(source, run_id="r2", principal_id="alice", scopes=h.SCOPES,
                            transport=fixture_transport(h.native_pages(h.GOOGLE)), secret=FIXTURE_SECRET)
    assert again["counts"]["new"] == 0 and again["counts"]["unchanged"] == 4
    assert len(again["receipts"]) == 2 and all(r["receipt"]["requests"] for r in again["receipts"])
    with pytest.raises(FactCheckError):
        monitor.refresh(source, run_id="r3", principal_id="alice", scopes=h.READ_ONLY,
                        transport=fixture_transport(h.native_pages(h.GOOGLE)), secret=FIXTURE_SECRET)


def test_live_revisions_from_unverified_providers_are_withheld():
    conn = h.connection()
    h.load_all(conn)
    conn.execute("UPDATE fact_check_revisions SET evidence_origin='live'")
    monitor = FactCheckMonitor(conn)
    watch = monitor.create(h.NS, "fd", watch="publisher", key="factdesk.example", principal_id="alice",
                           scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    result = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert result["notifications"] == [] and result["withheld_unverified_live_revisions"] == 3
