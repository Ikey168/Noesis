"""Monitor cited URLs for new captures and link rot through subscriptions (#2325, WA12)."""

from __future__ import annotations

import json

from src.kb.subscriptions import SubscriptionStore
from src.kb.web_archive_monitoring import CONTRACT, WebArchiveMonitor
from tests.unit import web_archive_harness as h

NEW_MEMENTO = "http://web.archive.org/web/20240501120000/https://example.org/report"


def _transport(*, live=200, pinned=200, extra_memento=False):
    timemap = json.loads(h.text("timetravel_timemap.json"))
    if extra_memento:
        timemap["mementos"]["list"].append({"datetime": "2024-05-01T12:00:00Z", "uri": NEW_MEMENTO})
    return h.FakeArchives({
        "https://timetravel.mementoweb.org/timemap/json/": {"status": 200, "headers": {},
                                                            "content": json.dumps(timemap).encode()},
        h.URL: {"status": live, "headers": {}, "content": b""},
        "http://web.archive.org/web/": {"status": pinned, "headers": {}, "content": b""},
    })


def _setup():
    conn = h.connect()
    clock = h.clock()
    client = h.client(conn, _transport(), now=clock)
    client.resolve(h.URL, request_id="seed")
    ia = next(c for c in client.store.captures_for_url(h.NS, h.URL, scopes=h.SCOPES)
              if c["memento_datetime"] == "2024-02-15T09:30:00Z")
    client.store.pin_citation(h.NS, "cite:1", ia["capture_id"], h.URL, principal_id="alice", scopes=h.SCOPES)
    monitor = WebArchiveMonitor(conn, now=clock)
    created = monitor.create(h.NS, "m1", h.URL, citation_id="cite:1", principal_id="alice", scopes=h.SCOPES)
    return conn, monitor, created["subscription_id"]


def _round(conn, monitor, sid, watermark, transport):
    checked = monitor.check(sid, request_id=f"check-{watermark}", principal_id="alice", scopes=h.SCOPES,
                            transport=transport, evidence_origin="fixture")
    SubscriptionStore(conn).commit_watermark(h.NS, watermark, kind="ingestion")
    return checked, monitor.run(sid, watermark, principal_id="alice", scopes=h.SCOPES)


def test_unchanged_timemaps_and_healthy_urls_emit_nothing_after_the_baseline():
    conn, monitor, sid = _setup()
    checked, first = _round(conn, monitor, sid, 1, _transport())
    assert first["baseline"] is True and first["notifications"] == []
    assert checked["observed"]["live"]["http_status"] == 200 and checked["observed"]["pinned"][0]["http_status"] == 200
    assert checked["archives"]["archive-today"] == "excluded_by_access_decision"
    _, second = _round(conn, monitor, sid, 2, _transport())
    assert second["events"] == 0 and second["notifications"] == []


def test_each_event_type_carries_its_record_ids_and_reports_outcomes_as_observed():
    conn, monitor, sid = _setup()
    _round(conn, monitor, sid, 1, _transport())
    _, changed = _round(conn, monitor, sid, 2, _transport(live=404, extra_memento=True))
    kinds = {n["kind"]: n for n in changed["notifications"]}
    assert set(kinds) == {"new_capture", "live_url_failure"}
    new = kinds["new_capture"]
    assert new["contract"] == CONTRACT and new["cites"]["capture_id"].startswith("web-archive-capture:")
    assert new["cites"]["uri_m"] == NEW_MEMENTO
    rot = kinds["live_url_failure"]
    assert rot["message"].startswith("HTTP 404 observed") and rot["cites"]["citation_id"] == "cite:1"
    assert rot["cites"]["pin_id"].startswith("citation-pin:") and rot["cites"]["health_id"]
    assert rot["interpretation"] == "outcome as observed; no conclusion about the publisher"
    _, gone = _round(conn, monitor, sid, 3, _transport(live=404, pinned=503, extra_memento=True))
    (unavailable,) = gone["notifications"]
    assert unavailable["kind"] == "pinned_capture_unavailable"
    assert unavailable["message"].startswith("HTTP 503 observed")
    assert unavailable["cites"]["capture_id"] and unavailable["cites"]["pin_id"]
    polled = monitor.poll(sid, principal_id="alice", scopes=h.SCOPES)
    assert polled["events"] and all(e["watermark"] in {1, 2, 3} for e in polled["events"])


def test_checks_stay_within_the_wa01_budget():
    conn, monitor, sid = _setup()
    transport = _transport()
    monitor.check(sid, request_id="budget", principal_id="alice", scopes=h.SCOPES, transport=transport)
    hosts = transport.hosts()
    assert hosts.count("timetravel.mementoweb.org") == 1  # one resolution
    assert hosts.count("example.org") == 1  # one live-URL check
    assert hosts.count("web.archive.org") == 1  # one check of the pinned capture
