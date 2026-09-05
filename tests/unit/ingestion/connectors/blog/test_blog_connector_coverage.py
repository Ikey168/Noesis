"""Real coverage tests for the blog / RSS / Atom connector.

feedparser is not installed in this environment, so a small in-process fake
feedparser module is injected via ``sys.modules``. It returns exactly the
duck-typed structure the connector consumes (``parsed.feed``, ``parsed.entries``
where each entry is dict-like). All the connector's real logic — discover(),
fetch(), parse(), HTML stripping, full-text enrichment, stable ids, timestamp
conversion, subscription pass-throughs — is exercised with assertions on the
returned Document objects.
"""

from __future__ import annotations

import sys
import types

import pytest

import src.ingestion.connectors.blog.connector as bc
from src.ingestion.connectors.base import RawDocument, SourceRef
from src.ingestion.connectors.blog.connector import BlogConnector
from src.ingestion.connectors.blog.models import FeedSubscription
from services.ingest.common.document_model import Document


# --------------------------------------------------------------------------- #
# Fake feedparser: an entry is a plain dict (already .get-compatible).
# --------------------------------------------------------------------------- #

class _Feed(dict):
    """dict with .get already; used for parsed.feed."""


class _Parsed:
    def __init__(self, feed, entries):
        self.feed = feed
        self.entries = entries


def _install_fake_feedparser(monkeypatch, feed=None, entries=None):
    fake = types.ModuleType("feedparser")
    captured = {}

    def parse(data):
        captured["data"] = data
        return _Parsed(_Feed(feed or {}), entries or [])

    fake.parse = parse
    fake._captured = captured
    monkeypatch.setitem(sys.modules, "feedparser", fake)
    return fake


def _entry(**kwargs):
    """Build a feedparser-style entry as a dict."""
    return dict(kwargs)


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #

def test_strip_html_removes_tags_and_collapses_whitespace():
    assert bc._strip_html("<p>Hello   <b>world</b></p>") == "Hello world"
    assert bc._strip_html(None) == ""
    assert bc._strip_html("") == ""


def test_to_millis_converts_struct_time():
    import time

    st = time.struct_time((2021, 1, 1, 0, 0, 0, 0, 1, 0))
    ms = bc._to_millis(st)
    assert ms == 1609459200000  # 2021-01-01T00:00:00Z in ms


def test_to_millis_none_and_bad_input():
    assert bc._to_millis(None) is None
    assert bc._to_millis("not-a-struct-time") is None


def test_stable_id_is_deterministic_and_prefixed():
    a = bc._stable_id("https://example.com/post", "Title")
    b = bc._stable_id("https://example.com/post", "Different")
    assert a.startswith("blog-")
    assert a == b  # url wins over title
    # Falls back to title when url is empty.
    t = bc._stable_id("", "Some Title")
    assert t.startswith("blog-")
    assert t != a


def test_normalize_query_accepts_strings_dicts_and_objects():
    sub = FeedSubscription(url="https://obj.example/feed", name="Obj", tags=["x"])
    result = bc._normalize_query([
        "https://str.example/feed",
        {"url": "https://dict.example/feed", "name": "Dict", "tags": ["t1"]},
        sub,
    ])
    assert len(result) == 3
    assert result[0].url == "https://str.example/feed"
    assert result[0].name == "https://str.example/feed"
    assert result[1].name == "Dict"
    assert result[1].tags == ["t1"]
    assert result[2] is sub


def test_normalize_query_dict_defaults_name_to_url():
    result = bc._normalize_query([{"url": "https://d.example/f"}])
    assert result[0].name == "https://d.example/f"
    assert result[0].tags == []


# --------------------------------------------------------------------------- #
# discover().
# --------------------------------------------------------------------------- #

def test_discover_with_query_yields_sourcerefs():
    conn = BlogConnector(fetch_full_text=False, http_get=lambda u: b"", full_text_getter=lambda u: "")
    refs = list(conn.discover([
        {"url": "https://a.example/feed", "name": "A", "tags": ["news"]},
        "https://b.example/feed",
    ]))
    assert len(refs) == 2
    assert isinstance(refs[0], SourceRef)
    assert refs[0].locator == "https://a.example/feed"
    assert refs[0].title == "A"
    assert refs[0].metadata["name"] == "A"
    assert refs[0].metadata["tags"] == ["news"]
    assert refs[1].locator == "https://b.example/feed"


def test_discover_without_query_reads_store(tmp_path):
    subs_path = tmp_path / "subs.json"
    conn = BlogConnector(
        subs_path=subs_path,
        fetch_full_text=False,
        http_get=lambda u: b"",
        full_text_getter=lambda u: "",
    )
    conn.subscribe("https://stored.example/feed", name="Stored", tags=["tech"])
    refs = list(conn.discover())
    assert len(refs) == 1
    assert refs[0].locator == "https://stored.example/feed"
    assert refs[0].title == "Stored"
    assert refs[0].metadata["tags"] == ["tech"]


# --------------------------------------------------------------------------- #
# fetch().
# --------------------------------------------------------------------------- #

def test_fetch_uses_injected_http_get():
    calls = {}

    def http_get(url):
        calls["url"] = url
        return b"<rss>bytes</rss>"

    conn = BlogConnector(fetch_full_text=False, http_get=http_get, full_text_getter=lambda u: "")
    ref = SourceRef(locator="https://feed.example/rss", title="Feed")
    raw = conn.fetch(ref)
    assert isinstance(raw, RawDocument)
    assert raw.content == b"<rss>bytes</rss>"
    assert raw.content_type == "application/xml"
    assert raw.ref is ref
    assert calls["url"] == "https://feed.example/rss"


# --------------------------------------------------------------------------- #
# parse(): the core normalization path.
# --------------------------------------------------------------------------- #

def test_parse_builds_documents_from_entries(monkeypatch):
    tag_obj = {"term": "AI"}
    entries = [
        _entry(
            title="First Post",
            link="https://blog.example/first",
            summary="<p>Summary <b>one</b></p>",
            author="Alice, Bob",
            tags=[tag_obj],
            published_parsed=__import__("time").struct_time((2022, 3, 4, 5, 6, 7, 0, 0, 0)),
        ),
        _entry(title="Second", link="https://blog.example/second", description="plain desc"),
    ]
    _install_fake_feedparser(monkeypatch, feed={"title": "Feed Title"}, entries=entries)

    conn = BlogConnector(fetch_full_text=False, http_get=lambda u: b"x", full_text_getter=lambda u: "")
    ref = SourceRef(locator="https://blog.example/rss", title="RSS", metadata={"name": "My Blog", "tags": ["curated"]})
    raw = RawDocument(ref=ref, content=b"<rss/>", content_type="application/xml", fetched_at=1700000000000)

    docs = conn.parse(raw)
    assert len(docs) == 2
    d0 = docs[0]
    assert isinstance(d0, Document)
    assert d0.source_type == "blog"
    assert d0.language == "en"
    assert d0.title == "First Post"
    assert d0.url == "https://blog.example/first"
    assert d0.content == "Summary one"  # HTML stripped from summary
    assert d0.authors == ["Alice", "Bob"]
    assert d0.source_id == "My Blog"
    assert d0.ingested_at == 1700000000000
    assert d0.created_at is not None  # published_parsed converted
    # feed tag "curated" plus entry tag "AI" merged in metadata.
    assert set(d0.metadata["tags"]) == {"curated", "AI"}
    assert d0.metadata["feed_name"] == "My Blog"
    assert d0.metadata["has_full_text"] is False
    # Stable id derives from url.
    assert d0.document_id == bc._stable_id("https://blog.example/first", "First Post")

    d1 = docs[1]
    assert d1.content == "plain desc"
    assert d1.authors == []


def test_parse_enriches_with_full_text(monkeypatch):
    entries = [_entry(title="Post", link="https://blog.example/post", summary="short summary")]
    _install_fake_feedparser(monkeypatch, feed={}, entries=entries)

    long_body = "This is the full article body. " * 20
    conn = BlogConnector(
        fetch_full_text=True,
        http_get=lambda u: b"x",
        full_text_getter=lambda url: long_body,
    )
    ref = SourceRef(locator="https://blog.example/rss", metadata={"name": "Blog", "tags": []})
    raw = RawDocument(ref=ref, content=b"<rss/>", fetched_at=1)

    docs = conn.parse(raw)
    assert len(docs) == 1
    assert docs[0].content == long_body  # full text replaced the short summary
    assert docs[0].metadata["has_full_text"] is True


def test_parse_full_text_error_falls_back_to_summary(monkeypatch):
    entries = [_entry(title="Post", link="https://blog.example/post", summary="fallback summary text")]
    _install_fake_feedparser(monkeypatch, feed={}, entries=entries)

    def boom(url):
        raise RuntimeError("network failure")

    conn = BlogConnector(fetch_full_text=True, http_get=lambda u: b"x", full_text_getter=boom)
    ref = SourceRef(locator="https://blog.example/rss", metadata={})
    raw = RawDocument(ref=ref, content=b"<rss/>", fetched_at=1)

    docs = conn.parse(raw)
    assert docs[0].content == "fallback summary text"
    assert docs[0].metadata["has_full_text"] is False


def test_parse_skips_entries_without_title_or_url(monkeypatch):
    entries = [
        _entry(summary="orphan with no title or link"),
        _entry(title="Kept", link="https://blog.example/kept", summary="body"),
    ]
    _install_fake_feedparser(monkeypatch, feed={}, entries=entries)

    conn = BlogConnector(fetch_full_text=False, http_get=lambda u: b"x", full_text_getter=lambda u: "")
    ref = SourceRef(locator="https://blog.example/rss", metadata={})
    raw = RawDocument(ref=ref, content="<rss/>", fetched_at=1)  # str content path

    docs = conn.parse(raw)
    assert len(docs) == 1
    assert docs[0].title == "Kept"


def test_parse_respects_limit_per_feed(monkeypatch):
    entries = [
        _entry(title=f"Post {i}", link=f"https://blog.example/{i}", summary="body")
        for i in range(5)
    ]
    _install_fake_feedparser(monkeypatch, feed={}, entries=entries)

    conn = BlogConnector(
        fetch_full_text=False, limit_per_feed=2,
        http_get=lambda u: b"x", full_text_getter=lambda u: "",
    )
    ref = SourceRef(locator="https://blog.example/rss", metadata={})
    raw = RawDocument(ref=ref, content=b"<rss/>", fetched_at=1)

    docs = conn.parse(raw)
    assert len(docs) == 2


def test_parse_uses_content_value_when_no_summary(monkeypatch):
    content_obj = types.SimpleNamespace(value="<div>Body from content field</div>")
    entries = [_entry(title="CPost", link="https://blog.example/c", content=[content_obj])]
    _install_fake_feedparser(monkeypatch, feed={}, entries=entries)

    conn = BlogConnector(fetch_full_text=False, http_get=lambda u: b"x", full_text_getter=lambda u: "")
    ref = SourceRef(locator="https://blog.example/rss", metadata={})
    raw = RawDocument(ref=ref, content=b"<rss/>", fetched_at=1)

    docs = conn.parse(raw)
    assert docs[0].content == "Body from content field"


def test_parse_feed_name_falls_back_to_parsed_feed_title(monkeypatch):
    entries = [_entry(title="P", link="https://blog.example/p", summary="body")]
    _install_fake_feedparser(monkeypatch, feed={"title": "Parsed Feed Name"}, entries=entries)

    conn = BlogConnector(fetch_full_text=False, http_get=lambda u: b"x", full_text_getter=lambda u: "")
    # No "name" in metadata -> should use parsed.feed title.
    ref = SourceRef(locator="https://blog.example/rss", metadata={})
    raw = RawDocument(ref=ref, content=b"<rss/>", fetched_at=1)

    docs = conn.parse(raw)
    assert docs[0].source_id == "Parsed Feed Name"


# --------------------------------------------------------------------------- #
# Subscription pass-throughs.
# --------------------------------------------------------------------------- #

def test_subscribe_unsubscribe_list_roundtrip(tmp_path):
    conn = BlogConnector(
        subs_path=tmp_path / "s.json",
        fetch_full_text=False,
        http_get=lambda u: b"",
        full_text_getter=lambda u: "",
    )
    sub = conn.subscribe("https://x.example/feed", name="X", tags=["a"])
    assert isinstance(sub, FeedSubscription)
    assert sub.url == "https://x.example/feed"

    listed = conn.list_subscriptions()
    assert [s.url for s in listed] == ["https://x.example/feed"]

    assert conn.unsubscribe("https://x.example/feed") is True
    assert conn.list_subscriptions() == []
    assert conn.unsubscribe("https://x.example/feed") is False


# --------------------------------------------------------------------------- #
# ingest_to_kg graceful failure.
# --------------------------------------------------------------------------- #

def test_ingest_to_kg_returns_zeros_when_kg_unavailable(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "src.knowledge_graph.enhanced_entity_extractor", None)
    conn = BlogConnector(fetch_full_text=False, http_get=lambda u: b"", full_text_getter=lambda u: "")
    doc = Document(
        document_id="blog-abc",
        source_type="blog",
        language="en",
        ingested_at=1,
        title="Title",
        content="Some content about entities.",
    )
    # Simulate an unavailable optional dependency regardless of installed models.
    result = conn.ingest_to_kg(doc)
    assert result == {"entities": 0, "relationships": 0}


# --------------------------------------------------------------------------- #
# harvest() end-to-end through discover -> fetch -> parse.
# --------------------------------------------------------------------------- #

def test_harvest_end_to_end(monkeypatch):
    entries = [_entry(title="Harvested", link="https://blog.example/h", summary="harvest body")]
    _install_fake_feedparser(monkeypatch, feed={}, entries=entries)

    conn = BlogConnector(
        fetch_full_text=False,
        http_get=lambda u: b"<rss/>",
        full_text_getter=lambda u: "",
    )
    docs = list(conn.harvest([{"url": "https://blog.example/rss", "name": "Blog", "tags": ["t"]}]))
    assert len(docs) == 1
    assert docs[0].title == "Harvested"
    assert docs[0].source_id == "Blog"
    assert "t" in docs[0].metadata["tags"]


# --------------------------------------------------------------------------- #
# Default full_text_getter wiring (readability import path).
# --------------------------------------------------------------------------- #

def test_default_full_text_getter_is_wired():
    from src.ingestion.connectors.blog.readability import fetch_full_text

    conn = BlogConnector(fetch_full_text=True, http_get=lambda u: b"")
    assert conn._full_text_getter is fetch_full_text


def test_default_http_get_is_wired():
    conn = BlogConnector(fetch_full_text=False, full_text_getter=lambda u: "")
    assert conn._http_get is bc._default_http_get
