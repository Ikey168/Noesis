"""Monitor authority revisions, merges, redirects and deprecations through subscriptions (#2225, MM10)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.media_metadata_sources import fixture_transport
from src.kb.media_metadata import MediaMetadataError
from src.kb.media_metadata_monitoring import MediaMetadataMonitor, identifier_key
from tests.unit import media_metadata_harness as h


@pytest.fixture()
def env():
    value = h.Env()
    assert value.run()["status"] == "complete"
    yield value
    value.conn.close()


def monitor(env):
    return MediaMetadataMonitor(env.conn, now=lambda: next(env.clock))


def newer_wikidata_author():
    pages = h.pages("wikidata-media")
    page = next(p for p in pages if p["request"].endswith("ids=Q999900001&maxlag=5&props=info%7Clabels%7Cclaims"))
    entity = page["body"]["entities"]["Q999900001"]
    entity["lastrevid"], entity["modified"] = 2200000001, "2026-06-01T09:00:00Z"
    entity["labels"]["fr"] = {"language": "fr", "value": "Mara Quell"}
    return pages


def test_watches_are_validated(env):
    with pytest.raises(MediaMetadataError):
        monitor(env).create(h.NS, "k", watch={}, principal_id="analyst", scopes=h.ALL)
    with pytest.raises(MediaMetadataError):
        monitor(env).create(h.NS, "k", watch={"identifiers": ["978-3-00-000001-1"]}, principal_id="analyst",
                            scopes=h.ALL)
    assert identifier_key(f"mbid:{h.RECORDING_MBID.upper()}") == f"mbid:{h.RECORDING_MBID}"
    assert identifier_key(h.AUTHOR_QID) == f"wikidata:{h.AUTHOR_QID}"


def test_revisions_merges_redirects_and_deprecations_are_cited_events_heard_once(env):
    watcher = monitor(env)
    created = watcher.create(h.NS, "authorities", principal_id="analyst", scopes=h.ALL, watch={
        "records": [f"wikidata:{h.AUTHOR_QID}", f"dnb:{h.OLD_GND}", f"loc:{h.LCNAF}"],
        "identifiers": [h.OLD_RECORDING_MBID]})
    first = watcher.run(created["subscription_id"], principal_id="analyst", scopes=h.ALL)
    kinds = [n["kind"] for n in first["notifications"]]
    assert {"authority_revision", "redirect", "merge", "identifier_assertion"} <= set(kinds)
    merge = next(n for n in first["notifications"] if n["kind"] == "merge")
    assert merge["cites"]["revision"]["native_id"] == h.OLD_RECORDING_MBID
    assert merge["cites"]["target"]["native_id"] == h.RECORDING_MBID
    assert merge["cites"]["target"]["revision"]["revision_marker"].startswith("mb-core:")
    redirect = next(n for n in first["notifications"] if n["kind"] == "redirect")
    assert redirect["cites"]["target"]["native_id"] == h.GND and redirect["date"] == "2025-12-01"
    revisions = [n for n in first["notifications"] if n["kind"] == "authority_revision"
                 and n["cites"]["revision"]["native_id"] == h.AUTHOR_QID]
    assert [n["cites"]["revision"]["revision_marker"] for n in revisions] == ["2000000001", "2100000001"]
    assert revisions[1]["cites"]["previous_revision"]["revision_marker"] == "2000000001"
    again = watcher.run(created["subscription_id"], principal_id="analyst", scopes=h.ALL)
    assert again["notifications"] == []  # replaying a watermark emits nothing

    # A new Wikidata revision, acquired through the bounded refresh, is one cited revision event.
    receipt = watcher.refresh(h.NS, h.source("wikidata-media"), principal_id="analyst", scopes=h.ALL,
                              transport=fixture_transport(newer_wikidata_author()))
    assert receipt["status"] == "complete" and receipt["new_revisions"] == 1 and receipt["min_interval_ms"] == 500
    later = watcher.run(created["subscription_id"], principal_id="analyst", scopes=h.ALL)
    assert [(n["kind"], n["cites"]["revision"]["revision_marker"], n["cites"]["previous_revision"]["revision_marker"])
            for n in later["notifications"]] == [("authority_revision", "2200000001", "2100000001")]
    assert later["notifications"][0]["date"] == "2026-06-01"

    # The Library of Congress deletes the heading: a deprecation citing both revisions.
    pages = h.pages("loc-lcnaf-media")
    pages[0]["body"] = pages[0]["body"].replace("00000cz", "00000dz").replace("20260110120000.0",
                                                                              "20260702080000.0")
    watcher.refresh(h.NS, h.source("loc-lcnaf-media"), principal_id="analyst", scopes=h.ALL,
                    transport=fixture_transport(pages))
    deprecated = watcher.run(created["subscription_id"], principal_id="analyst", scopes=h.ALL)["notifications"]
    assert {n["kind"] for n in deprecated} == {"authority_revision", "deprecation"}
    event = next(n for n in deprecated if n["kind"] == "deprecation")
    assert event["status"] == "deleted" and event["cites"]["previous_revision"]["revision_marker"] == \
        "20260110120000.0" and event["cites"]["revision"]["revision_marker"] == "20260702080000.0"
    polled = watcher.poll(created["subscription_id"], principal_id="analyst", scopes=h.ALL)
    assert polled["events"]


def test_refresh_is_bounded_receipted_and_respects_rate_limits(env):
    watcher = monitor(env)
    unchanged = watcher.refresh(h.NS, h.source("openlibrary-media"), principal_id="analyst", scopes=h.ALL,
                                transport=fixture_transport(h.pages("openlibrary-media")), max_pages=3)
    assert unchanged["status"] == "bounded" and len(unchanged["pages"]) == 3 and unchanged["new_revisions"] == 0
    throttled = [{**p, "status": 503, "headers": {"Retry-After": "120"}} for p in h.pages("musicbrainz-media")]
    stopped = watcher.refresh(h.NS, h.source("musicbrainz-media"), principal_id="analyst", scopes=h.ALL,
                              transport=fixture_transport(throttled))
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited" and stopped["retry_at"]
    waiting = watcher.refresh(h.NS, h.source("musicbrainz-media"), principal_id="analyst", scopes=h.ALL,
                              transport=fixture_transport(h.pages("musicbrainz-media")))
    assert waiting["status"] == "rate_limited_wait" and waiting["pages"] == []
    receipts = env.conn.execute("SELECT receipt_json FROM media_refresh_receipts ORDER BY started_at_ms").fetchall()
    assert [json.loads(r[0])["status"] for r in receipts] == ["bounded", "stopped", "rate_limited_wait"]
    with pytest.raises(MediaMetadataError):
        watcher.refresh(h.NS, {"source_id": "x", "connector": "ddb"}, principal_id="analyst", scopes=h.ALL)
