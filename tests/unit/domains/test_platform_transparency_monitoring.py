"""Platform-transparency monitors: new, revised, removed and unchanged ads and dump releases (#2635)."""

from __future__ import annotations

import pytest

from src.ingestion.platform_transparency_sources import fixture_transport
from src.kb.platform_transparency_links import PlatformTransparencyLinks
from src.kb.platform_transparency_monitoring import PlatformTransparencyMonitor
from src.kb.platform_transparency_records import PlatformTransparencyError
from src.kb.subscriptions import SubscriptionStore
from tests.unit import platform_transparency_harness as h


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_advertiser_election_and_platform_monitors_report_new_revised_removed_and_unchanged():
    conn = h.accepted_world()
    links = PlatformTransparencyLinks(conn)
    links.link_all(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    monitor = PlatformTransparencyMonitor(conn)
    watch = {
        "advertiser": monitor.create(h.NS, "cmte", watch="advertiser", key="C00999901", principal_id="alice",
                                     scopes=h.SCOPES),
        "election": monitor.create(h.NS, "uk", watch="election", key=h.UK_ELECTION, principal_id="alice",
                                   scopes=h.SCOPES),
        "platform": monitor.create(h.NS, "exampla", watch="platform", key="Exampla Social", principal_id="alice",
                                   scopes=h.SCOPES),
    }
    assert "no new scheduler" in watch["advertiser"]["refresh"]
    with pytest.raises(PlatformTransparencyError):
        monitor.run(watch["advertiser"]["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    first = {k: monitor.run(v["subscription_id"], principal_id="alice", scopes=h.SCOPES) for k, v in watch.items()}
    assert kinds(first["advertiser"]) == ["new_ad"] * 3
    assert kinds(first["election"]) == ["new_ad"] * 5
    assert kinds(first["platform"]) == ["new_dump_release"] * 2
    h.load_all(conn, version="v2", run_id="run:v2")
    links.link_all(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    second = {k: monitor.run(v["subscription_id"], principal_id="alice", scopes=h.SCOPES) for k, v in watch.items()}
    assert kinds(second["advertiser"]) == ["ad_not_returned", "ad_revised", "new_ad"]
    revised = next(n for n in second["advertiser"]["notifications"] if n["kind"] == "ad_revised")
    assert revised["changed"]["impressions_as_published"] == {"before": "100k-1M", "after": "1M-10M"}
    assert revised["cites"]["previous_revision_id"] and revised["cites"]["previous_revision_id"] != \
        revised["cites"]["revision_id"]
    removed = next(n for n in second["advertiser"]["notifications"] if n["kind"] == "ad_not_returned")
    assert removed["removal_revision_id"] == removed["cites"]["revision_id"] and "not a stated deletion" in \
        removed["message"]
    # Google ads of the UK party were only refreshed: no notice; the Meta changes are notified
    assert kinds(second["election"]) == ["ad_not_returned", "ad_revised"]
    republished = second["platform"]["notifications"]
    assert [n["kind"] for n in republished] == ["dump_republished"]
    assert republished[0]["before"]["statements"] == 5 and republished[0]["after"]["statements"] == 4
    SubscriptionStore(conn).commit_watermark(h.NS, 3)
    assert all(monitor.run(v["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
               for v in watch.values())
    assert monitor.poll(watch["platform"]["subscription_id"], principal_id="alice", scopes=h.SCOPES)["events"]
    with pytest.raises(PlatformTransparencyError):
        monitor.create(h.NS, "bad", watch="election", key="not-an-election", principal_id="alice", scopes=h.SCOPES)


def test_refresh_is_bounded_idempotent_receipted_and_unverified_live_revisions_are_withheld():
    conn = h.connection()
    monitor = PlatformTransparencyMonitor(conn, now=lambda: h.T1)
    source = h.source("meta-ad-library-political")
    pages = h.native_pages("meta-ad-library-political")
    first = monitor.refresh(source, run_id="refresh-1", principal_id="op", scopes=h.SCOPES,
                            transport=fixture_transport(pages), secret="token")
    assert first["units"] == 3 and first["complete"] and first["counts"]["new"] == 9
    assert len(first["receipts"]) == 3
    again = monitor.refresh(source, run_id="refresh-2", principal_id="op", scopes=h.SCOPES,
                            transport=fixture_transport(pages), secret="token")
    assert again["counts"]["new"] == 0 and again["counts"]["unchanged"] == 9
    live = [dict(p) for p in pages]

    def live_transport(**kwargs):
        response = dict(fixture_transport(live)(**kwargs))
        response["origin"] = "live"
        return response

    other = h.connection()
    live_monitor = PlatformTransparencyMonitor(other, now=lambda: h.T1)
    live_monitor.refresh(source, run_id="live-1", principal_id="op", scopes=h.SCOPES, transport=live_transport,
                         secret="token")
    watch = live_monitor.create(h.NS, "fund", watch="advertiser", key=h.META_FUND, principal_id="alice",
                                scopes=h.SCOPES)
    SubscriptionStore(other).commit_watermark(h.NS, 1)
    result = live_monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert result["notifications"] == [] and result["withheld_unverified_live_revisions"] == 2
