"""Platform-transparency monitors: new, revised, removed and unchanged ads and dump releases (#2635)."""

from __future__ import annotations

import pytest

from src.ingestion.platform_transparency_sources import (
    FIXTURE_SECRET,
    fixture_transport,
)
from src.kb.platform_transparency_links import PlatformTransparencyLinks
from src.kb.platform_transparency_monitoring import PlatformTransparencyMonitor
from src.kb.platform_transparency_records import (
    PlatformTransparencyError,
    forbidden_keys,
)
from src.kb.subscriptions import SubscriptionStore
from tests.unit import platform_transparency_harness as h


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_advertiser_monitor_reports_new_revised_removed_and_unchanged_ads_with_citations():
    conn = h.connection()
    h.apply(conn, h.META, observed_at_ms=h.V1_MS)
    monitor = PlatformTransparencyMonitor(conn)
    party = monitor.create(h.NS, "party", watch="advertiser", key=h.PARTY_PAGE, principal_id="alice",
                           scopes=h.SCOPES)
    holdings = monitor.create(h.NS, "holdings", watch="advertiser", key=h.HOLDINGS_PAGE, principal_id="alice",
                              scopes=h.SCOPES)
    assert "no new scheduler" in party["refresh"]
    with pytest.raises(PlatformTransparencyError):
        monitor.run(party["subscription_id"], principal_id="alice", scopes=h.SCOPES)  # no committed watermark
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    first = monitor.run(party["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(first) == ["ad_published", "ad_published"]
    assert all(n["cites"]["revision_id"] and n["cites"]["previous_revision_id"] is None
               for n in first["notifications"])
    monitor.run(holdings["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    h.apply(conn, h.META, version="v2", run_id="run:v2", observed_at_ms=h.V2_MS)
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    second = monitor.run(party["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(second) == ["ad_published", "ad_ranges_revised"]
    revised = next(n for n in second["notifications"] if n["kind"] == "ad_ranges_revised")
    assert revised["before"]["spend_range_as_published"]["lower_bound"] == "1000"
    assert revised["after"]["spend_range_as_published"]["lower_bound"] == "1500"
    assert revised["cites"]["previous_revision_id"] != revised["cites"]["revision_id"]
    removed = monitor.run(holdings["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(removed) == ["ad_not_returned"]
    assert removed["notifications"][0]["basis"]["listing_record_key"]
    SubscriptionStore(conn).commit_watermark(h.NS, 3)
    assert monitor.run(party["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    for result in (first, second, removed):
        assert forbidden_keys(result) == []
    assert monitor.poll(party["subscription_id"], principal_id="alice", scopes=h.SCOPES)["events"]


def test_platform_monitor_reports_dump_releases_and_republished_dumps():
    conn = h.connection()
    monitor = PlatformTransparencyMonitor(conn)
    watch = monitor.create(h.NS, "video", watch="platform", key="Example Video", principal_id="alice",
                           scopes=h.SCOPES)
    assert watch["query"]["key"] == "example-video"
    h.apply(conn, h.DSA, observed_at_ms=h.V1_MS)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    first = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(first) == ["dump_released", "dump_released"]
    h.apply(conn, h.DSA, version="v2", run_id="run:v2", observed_at_ms=h.V2_MS)
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    second = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    (note,) = second["notifications"]
    assert note["kind"] == "dump_republished" and note["before"] != note["after"]


def test_election_monitor_follows_links():
    conn = h.accepted_world()
    PlatformTransparencyLinks(conn).link_elections(h.NS, principal_id="alice", scopes=h.SCOPES,
                                                   elections_namespace=h.CF_NS)
    monitor = PlatformTransparencyMonitor(conn)
    watch = monitor.create(h.NS, "ge", watch="election", key="gb-general:2099-05-07", principal_id="alice",
                           scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    assert set(kinds(monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES))) == {
        "ad_published"}
    with pytest.raises(PlatformTransparencyError):
        monitor.create(h.NS, "bad", watch="user", key="someone", principal_id="alice", scopes=h.SCOPES)


def test_refresh_is_bounded_idempotent_and_leaves_receipts():
    conn = h.connection()
    monitor = PlatformTransparencyMonitor(conn)
    first = monitor.refresh(h.source(h.GOOGLE), run_id="refresh-1", principal_id="alice", scopes=h.SCOPES,
                            transport=fixture_transport(h.native_pages(h.GOOGLE)), secret=FIXTURE_SECRET)
    assert first["complete"] and first["units"] == 3 and first["counts"]["new"] == 10
    assert len(first["receipts"]) == 3 and all(r["receipt"]["requests"] for r in first["receipts"])
    again = monitor.refresh(h.source(h.GOOGLE), run_id="refresh-2", principal_id="alice", scopes=h.SCOPES,
                            transport=fixture_transport(h.native_pages(h.GOOGLE)), secret=FIXTURE_SECRET)
    assert again["counts"] == {"new": 0, "revised": 0, "not-returned": 0, "relisted": 0, "unchanged": 10,
                               "older-observation": 0}
    with pytest.raises(PlatformTransparencyError):
        monitor.refresh(h.source(h.GOOGLE), run_id="refresh-3", principal_id="bob", scopes=h.READ_ONLY,
                        transport=fixture_transport(h.native_pages(h.GOOGLE)), secret=FIXTURE_SECRET)


def test_live_revisions_of_unverified_providers_are_withheld_from_notices():
    conn = h.connection()
    h.apply(conn, h.META, observed_at_ms=h.V1_MS)
    conn.execute("UPDATE platform_transparency_revisions SET evidence_origin='live'")
    monitor = PlatformTransparencyMonitor(conn)
    watch = monitor.create(h.NS, "party", watch="advertiser", key=h.PARTY_PAGE, principal_id="alice",
                           scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    result = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert result["notifications"] == [] and result["withheld_unverified_live_revisions"] == 2
