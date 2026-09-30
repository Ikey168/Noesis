"""What a page said on a date and according to which archive (#2301, WA10)."""

from __future__ import annotations

import pytest

from src.kb.citation_preservation import CitationPreservationError
from src.kb.web_archive_queries import page_as_of
from tests.unit import web_archive_harness as h

AT = "2024-02-20T00:00:00Z"


def _resolved(transport=None):
    conn = h.connect()
    client = h.client(conn, transport or h.FakeArchives())
    client.resolve(h.URL, request_id="agg", crawls=[h.CRAWL])
    client.resolve(h.URL, request_id="direct", via_aggregator=False,
                   archives=["internet-archive", "uk-web-archive", "arquivo-pt", "archive-today"])
    return conn


def test_per_archive_nearest_prior_and_after_with_distance_and_citations():
    conn = _resolved()
    answer = page_as_of(conn, h.NS, h.URL, AT, scopes=h.READ_ONLY)
    rows = {r["archive_id"]: r for r in answer["archives"]}
    ia = rows["internet-archive"]
    assert ia["status"] == "captures_on_record" and ia["capture_count"] == 4
    assert ia["nearest_prior"]["memento_datetime"] == "2024-02-15T09:30:00Z"
    assert ia["nearest_prior"]["distance_seconds"] == 4 * 86400 + 14 * 3600 + 30 * 60
    assert ia["nearest_after"]["memento_datetime"] == "2024-03-01T00:00:00Z"
    assert ia["nearest_after"]["status"] == 301
    ukwa = rows["uk-web-archive"]
    assert ukwa["nearest_prior"]["memento_datetime"] == "2024-01-20T10:00:00Z"
    assert ukwa["nearest_after"]["access_condition"] == "reading-room-only"
    crawl = rows["common-crawl"]
    assert crawl["archive_kind"] == "crawl-corpus" and crawl["timegate_answer"] is False
    assert crawl["nearest_after"]["memento_datetime"] == "2024-02-20T14:30:00Z"
    # The crawl record is closer, but only Memento archives answer the as-of question.
    assert answer["closest"]["archive_id"] == "internet-archive" and answer["status"] == "answered"
    assert ia["nearest_prior"]["capture_id"] in answer["cites"]["capture_ids"]
    assert set(ia["timemap_ids"]) <= set(answer["cites"]["timemap_ids"])


def test_identical_digests_group_as_same_content_without_dropping_either_archive():
    answer = page_as_of(_resolved(), h.NS, h.URL, AT, scopes=h.READ_ONLY)
    (group,) = answer["same_content_groups"]
    assert group["archives"] == ["common-crawl", "internet-archive"] and group["digest"] == "Q" * 31 + "7"
    rows = {r["archive_id"]: r for r in answer["archives"]}
    assert rows["internet-archive"]["nearest_prior"] and rows["common-crawl"]["nearest_after"]


def test_no_capture_unavailable_and_excluded_are_distinct_outcomes():
    unavailable = h.FakeArchives({"https://www.webarchive.org.uk/wayback/archive/timemap/link/":
                                  {"status": 503, "headers": {}, "content": b""}})
    conn = h.connect()
    h.client(conn, unavailable).resolve(h.URL, request_id="d", via_aggregator=False,
                                        archives=["uk-web-archive", "arquivo-pt"])
    rows = {r["archive_id"]: r for r in page_as_of(conn, h.NS, h.URL, AT, scopes=h.READ_ONLY)["archives"]}
    assert rows["arquivo-pt"]["status"] == "no_capture_on_record"
    assert rows["uk-web-archive"]["status"] == "archive_unavailable"
    assert rows["archive-today"]["status"] == "excluded_by_access_decision"
    assert rows["library-of-congress"]["status"] == "deferred_by_access_decision"
    assert rows["internet-archive"]["status"] == "not_queried"
    empty = page_as_of(conn, h.NS, h.URL, AT, scopes=h.READ_ONLY)
    assert empty["status"] == "no_capture_on_record"
    assert empty["statement"] == "no archive holds a capture of this URL on record"
    assert empty["closest"] is None and empty["same_content_groups"] == []


def test_aggregator_silence_is_not_reported_as_no_capture():
    conn = h.connect()
    h.client(conn).resolve(h.URL, request_id="agg")
    rows = {r["archive_id"]: r for r in page_as_of(conn, h.NS, h.URL, AT, scopes=h.READ_ONLY)["archives"]}
    assert rows["arquivo-pt"]["status"] == "not_reported_by_aggregator"
    with pytest.raises(CitationPreservationError):
        page_as_of(conn, h.NS, h.URL, AT, scopes={"knowledge:read"})


def test_a_variant_of_the_url_finds_the_same_captures_and_labels_the_match():
    answer = page_as_of(_resolved(), h.NS, h.CITED, AT, scopes=h.READ_ONLY)
    prior = {r["archive_id"]: r for r in answer["archives"]}["internet-archive"]["nearest_prior"]
    assert prior["match_kind"] == "canonicalised" and "strip-www" in prior["match_rules"]
