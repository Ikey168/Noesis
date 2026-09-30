"""Internet Archive Memento captures with CDX digests (#2256, WA04)."""

from __future__ import annotations

import inspect

from src.ingestion import wayback
from src.kb.citation_preservation import READ_SCOPE
from tests.unit import web_archive_harness as h


def test_timemap_and_cdx_map_to_capture_records_with_published_digests():
    conn = h.connect()
    transport = h.FakeArchives()
    client = h.client(conn, transport)
    result = client.resolve_archive(h.URL, "internet-archive", request_id="ia")
    assert result["outcome"] == "captures" and result["cdx"] == "read"
    assert len(result["capture_ids"]) == 4  # three TimeMap mementos plus a CDX-only redirect capture
    assert [c["url"].split("/")[2] for c in transport.calls] == ["web.archive.org", "web.archive.org"]
    captures = {c["memento_datetime"]: c for c in client.store.captures_for_url(h.NS, h.URL, scopes={READ_SCOPE})}
    feb = captures["2024-02-15T09:30:00Z"]
    assert feb["archive_id"] == "internet-archive" and feb["resolver"] == "internet-archive"
    assert feb["status"] == 200 and feb["mimetype"] == "text/html"
    assert feb["digests"] == [{"algorithm": "sha1-base32", "value": "Q" * 31 + "7", "basis": "published"}]
    assert feb["uri_m"] == h.IA_MEMENTO
    redirect = captures["2024-03-01T00:00:00Z"]
    assert redirect["archive_redirect"] == {"status": 301, "location": "https://example.org/reports/2024"}
    assert redirect["receipt"]["adapter"] == "wayback-memento-v1"


def test_mementos_from_the_aggregator_and_from_the_archive_share_one_record_per_uri_m():
    conn = h.connect()
    client = h.client(conn, h.FakeArchives())
    client.resolve(h.URL, request_id="agg")
    client.resolve_archive(h.URL, "internet-archive", request_id="ia")
    rows = conn.execute("SELECT count(*) FROM web_archive_captures WHERE archive_id='internet-archive'").fetchone()
    assert rows[0] == 4
    feb = next(c for c in client.store.captures_for_url(h.NS, h.URL, scopes={READ_SCOPE})
               if c["uri_m"] == h.IA_MEMENTO)
    assert feb["resolver"] == "timetravel" and feb["enrichments"][0]["fields"] == ["digests", "mimetype", "status"]


def test_excluded_or_robots_blocked_urls_are_recorded_unavailable_and_never_retried_elsewhere():
    conn = h.connect()
    transport = h.FakeArchives({"https://web.archive.org/cdx/search/cdx": {
        "status": 403, "headers": {}, "content": b"org.archive.wayback.exception.AdministrativeAccessControlException: "
                                                  b"Blocked Site Error"}})
    result = h.client(conn, transport).resolve_archive(h.URL, "internet-archive", request_id="blocked")
    assert result["outcome"] == "excluded_by_archive" and result["detail"]["retry_elsewhere"] is False
    assert conn.execute("SELECT count(*) FROM web_archive_captures").fetchone()[0] == 0
    assert {c["url"].split("/")[2] for c in transport.calls} == {"web.archive.org"}
    timemap_blocked = h.FakeArchives({"https://web.archive.org/web/timemap/link/":
                                      {"status": 403, "headers": {}, "content": b"Blocked Site Error"}})
    blocked = h.client(h.connect(), timemap_blocked).resolve_archive(h.URL, "internet-archive", request_id="b2")
    assert blocked["outcome"] == "excluded_by_archive" and len(timemap_blocked.calls) == 1


def test_unavailable_cdx_keeps_timemap_captures_without_invented_digests():
    conn = h.connect()
    transport = h.FakeArchives({"https://web.archive.org/cdx/search/cdx": {"status": 502, "headers": {},
                                                                        "content": b""}})
    client = h.client(conn, transport)
    result = client.resolve_archive(h.URL, "internet-archive", request_id="nocdx")
    assert result["outcome"] == "captures" and result["cdx"] == "unavailable" and len(result["capture_ids"]) == 3
    assert all(c["digests"] == [] and c["status"] is None
               for c in client.store.captures_for_url(h.NS, h.URL, scopes={READ_SCOPE}))


def test_existing_acquire_wayback_callers_keep_their_behaviour():
    signature = inspect.signature(wayback.acquire_wayback)
    assert list(signature.parameters) == ["conn", "url", "timestamp", "request_id", "language", "transport",
                                          "max_bytes", "timeout_s"]
    assert "wayback-availability-v1" in inspect.getsource(wayback.acquire_wayback)
