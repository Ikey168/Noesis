"""Common Crawl index hits as crawl-corpus capture records (#2287, WA07)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.common_crawl import CommonCrawlCollection, lookup_url_captures
from src.kb.citation_preservation import READ_SCOPE
from tests.unit import web_archive_harness as h


def test_index_hits_map_to_crawl_corpus_captures_with_crawl_digest_and_warc_locator():
    conn = h.connect()
    transport = h.FakeArchives()
    client = h.client(conn, transport)
    result = lookup_url_captures(client, h.URL, crawl=h.CRAWL, request_id="cc")
    assert result["outcome"] == "captures" and len(result["capture_ids"]) == 1
    (call,) = transport.calls
    assert call["url"] == "https://index.commoncrawl.org/CC-MAIN-2024-10-index"
    assert call["params"]["url"] == h.URL and call["params"]["output"] == "json"
    (capture,) = client.store.captures_for_url(h.NS, h.URL, scopes={READ_SCOPE})
    assert capture["archive_id"] == "common-crawl" and capture["archive_kind"] == "crawl-corpus"
    assert capture["resolver"] == "common-crawl-index:CC-MAIN-2024-10"
    assert capture["memento_datetime"] == "2024-02-20T14:30:00Z" and capture["status"] == 200
    assert capture["digests"] == [{"algorithm": "sha1-base32", "value": "Q" * 31 + "7", "basis": "published"}]
    assert capture["warc_locator"]["crawl"] == h.CRAWL and capture["warc_locator"]["offset"] == 1048576
    assert capture["access_condition"] == "not-replayable"


def test_common_crawl_is_never_a_timegate_answer_and_is_bounded_to_index_lookups():
    conn = h.connect()
    transport = h.FakeArchives()
    result = h.client(conn, transport).resolve(h.URL, request_id="with-cc", at="2024-03-01", crawls=[h.CRAWL])
    crawl = result["archives"]["common-crawl"]
    assert crawl["archive_kind"] == "crawl-corpus" and crawl["timegate_answer"] is False
    assert crawl["outcome"] == "captures"
    assert result["timegate"]["archive_id"] == "internet-archive"
    assert "data.commoncrawl.org" not in transport.hosts()
    assert transport.hosts().count("index.commoncrawl.org") == 1
    with pytest.raises(Exception, match="too many crawls"):
        h.client(h.connect()).resolve(h.URL, request_id="x", crawls=["CC-MAIN-2024-10", "CC-MAIN-2024-18",
                                                                     "CC-MAIN-2024-22"])


def test_index_miss_and_throttling_are_distinct_outcomes():
    missing = h.FakeArchives({"https://index.commoncrawl.org/": {"status": 404, "headers": {},
                                                                 "content": b'{"message": "No Captures found"}'}})
    assert lookup_url_captures(h.client(h.connect(), missing), h.URL, crawl=h.CRAWL,
                               request_id="m")["outcome"] == "no_capture_on_record"
    throttled = h.FakeArchives({"https://index.commoncrawl.org/": {"status": 503, "headers": {}, "content": b""}})
    assert lookup_url_captures(h.client(h.connect(), throttled), h.URL, crawl=h.CRAWL,
                               request_id="t")["outcome"] == "archive_unavailable"


def test_completed_collection_records_carry_published_and_computed_digests():
    conn = h.connect()
    collection = CommonCrawlCollection(conn, "coll", crawl=h.CRAWL, host="example.org",
                                       from_timestamp="20240101000000", to_timestamp="20241231000000")
    item = json.loads(h.text("cc_index.jsonl"))
    state = collection.inspect()
    state["completed"].append({"crawl": h.CRAWL, "index_entry": item, "archive": {}})
    collection._save(state)
    client = h.client(conn)
    (capture_id,) = collection.capture_records(client)
    capture = client.store.capture_record(h.NS, capture_id, scopes={READ_SCOPE})
    assert {d["basis"] for d in capture["digests"]} == {"published", "computed"}
