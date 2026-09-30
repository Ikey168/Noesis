"""Offline acceptance: a cited URL and date resolve to captures in several archives and one is pinned (#2336, WA14).

Synthetic fixtures cover the Time Travel aggregator, the Internet Archive (TimeMap and CDX),
one national archive (UK Web Archive), Common Crawl, an archive excluded by access decision
(archive.today), an archive with no capture (Arquivo.pt) and an unavailable archive. No
network: the live transports are replaced by guards that fail the test if reached.
"""

from __future__ import annotations

import pytest

from src.ingestion import memento, wayback
from src.kb.citation_preservation import CitationPreservationStore
from src.kb.web_archive_identity import CaptureMatcher
from src.kb.web_archive_queries import page_as_of
from tests.unit import web_archive_harness as h

OTHER = "https://example.org/unreachable-report"
DATE = "2024-02-20T00:00:00Z"
CITATION = "legal:docket:70001:entry:3"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(**_):
        raise AssertionError("acceptance must not touch the network")

    monkeypatch.setattr(memento, "_request", refuse)
    monkeypatch.setattr(wayback, "_spn_request", refuse)


def _transport():
    return h.FakeArchives({
        "https://www.webarchive.org.uk/wayback/archive/timemap/link/" + OTHER:
            {"status": 503, "headers": {}, "content": b""},
        "https://web.archive.org/web/timemap/link/" + OTHER: {"status": 404, "headers": {}, "content": b""},
        "https://web.archive.org/cdx/search/cdx": {"status": 200, "headers": {}, "content": h.text("ia_cdx.json")},
    })


def test_cited_url_to_pinned_capture_in_the_evidence_export():
    conn = h.connect()
    transport = _transport()
    clock = h.clock()
    client = h.client(conn, transport, now=clock)

    # Resolve through the aggregator and each archive directly, plus one Common Crawl index lookup.
    via_aggregator = client.resolve(h.URL, request_id="acc:agg", at=DATE, crawls=[h.CRAWL])
    direct = client.resolve(h.URL, request_id="acc:direct", via_aggregator=False,
                            archives=["internet-archive", "uk-web-archive", "arquivo-pt", "archive-today"])
    assert via_aggregator["archives"]["archive-today"]["outcome"] == "excluded_by_access_decision"
    assert direct["archives"]["arquivo-pt"]["outcome"] == "no_capture_on_record"
    assert "archive.ph" not in transport.hosts()

    # Per-archive nearest captures around the date, in at least two archives.
    answer = page_as_of(conn, h.NS, h.CITED, DATE, scopes=h.READ_ONLY)
    rows = {r["archive_id"]: r for r in answer["archives"]}
    holding = [a for a, r in rows.items() if r["status"] == "captures_on_record" and r["timegate_answer"]]
    assert {"internet-archive", "uk-web-archive"} <= set(holding)
    assert rows["internet-archive"]["nearest_prior"]["memento_datetime"] == "2024-02-15T09:30:00Z"
    assert rows["internet-archive"]["nearest_after"]["memento_datetime"] == "2024-03-01T00:00:00Z"
    assert rows["uk-web-archive"]["nearest_prior"]["memento_datetime"] == "2024-01-20T10:00:00Z"
    assert rows["uk-web-archive"]["nearest_after"]["access_condition"] == "reading-room-only"
    assert rows["common-crawl"]["archive_kind"] == "crawl-corpus" and not rows["common-crawl"]["timegate_answer"]
    assert rows["arquivo-pt"]["status"] == "no_capture_on_record"
    assert rows["archive-today"]["status"] == "excluded_by_access_decision"
    # Digest grouping: the Internet Archive capture and the crawl record carry the same payload digest.
    (group,) = answer["same_content_groups"]
    assert group["archives"] == ["common-crawl", "internet-archive"]
    assert answer["closest"]["archive_id"] == "internet-archive"

    # The cited URL is a variant: the match is reviewable, and a reviewer confirms it.
    matcher = CaptureMatcher(conn, now=clock)
    proposal = matcher.propose(h.NS, CITATION, h.CITED, principal_id="alice", scopes=h.SCOPES)
    prior_id = rows["internet-archive"]["nearest_prior"]["capture_id"]
    match = next(m for m in proposal["matches"] if m["capture_id"] == prior_id)
    assert match["match_kind"] == "canonicalised" and match["state"] == "pending_review"
    accepted = matcher.review(h.NS, match["match_id"], "accept", "tracking parameters and www. only",
                              principal_id="bob", scopes=h.SCOPES)
    assert accepted["state"] == "accepted"

    # Pin and export: the pin sits next to the untouched cited URL.
    store = CitationPreservationStore(conn, now=clock)
    policy = store.register_policy(h.NS, "policy:web", "1", principal_id="curator", scopes=h.SCOPES)
    store.capture(h.NS, policy["policy_id"], CITATION, h.CITED, content=None, principal_id="curator",
                  scopes=h.SCOPES)
    pin = store.pin_citation(h.NS, CITATION, prior_id, h.CITED, principal_id="alice", scopes=h.SCOPES)
    assert pin["match"]["match_kind"] == "canonicalised"
    exported = store.export(h.NS, [CITATION], scopes=h.SCOPES)["items"][0]
    assert exported["snapshots"][0]["source_url"] == h.CITED
    assert exported["archive_pin"]["cited_url"] == h.CITED
    assert exported["archive_pin"]["archive_id"] == "internet-archive"
    assert exported["archive_pin"]["uri_m"] == h.IA_MEMENTO
    assert exported["archive_pin"]["memento_datetime"] == "2024-02-15T09:30:00Z"
    assert exported["archive_pin"]["digests"][0]["value"] == group["digest"]


def test_unknowns_are_distinct_outcomes():
    conn = h.connect()
    client = h.client(conn, _transport())
    client.resolve(OTHER, request_id="acc:other", via_aggregator=False,
                   archives=["internet-archive", "uk-web-archive", "archive-today"])
    rows = {r["archive_id"]: r["status"] for r in page_as_of(conn, h.NS, OTHER, DATE,
                                                              scopes=h.READ_ONLY)["archives"]}
    assert rows["internet-archive"] == "no_capture_on_record"
    assert rows["uk-web-archive"] == "archive_unavailable"
    assert rows["archive-today"] == "excluded_by_access_decision"
    assert rows["arquivo-pt"] == "not_queried"
    assert len({rows["internet-archive"], rows["uk-web-archive"], rows["archive-today"], rows["arquivo-pt"]}) == 4
