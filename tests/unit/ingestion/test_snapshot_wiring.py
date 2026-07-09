"""Unit tests for snapshot wiring: ingest hook, liveness, retention (#825)."""

from __future__ import annotations

import pytest

from services.ingest.common.document_model import Document
from src.ingestion.snapshots import (
    SnapshotStore,
    check_liveness,
    prune_snapshots,
    resolve_citation_live,
    snapshot_document,
)


@pytest.fixture()
def store():
    duckdb = pytest.importorskip("duckdb")
    return SnapshotStore(duckdb.connect(":memory:"))


def _doc(url="https://ex.com/story"):
    return Document(
        document_id="news:1", source_type="news", language="en",
        ingested_at=100, url=url, title="Story",
    )


def test_snapshot_document_archives_fetched_page(store):
    out = snapshot_document(store, _doc(), "<p>the article body</p>", fetched_at=100)
    assert out is not None and out["chars"] > 0
    snap = store.latest("https://ex.com/story")
    assert snap.text == "the article body"


def test_snapshot_document_noop_without_url(store):
    doc = Document(document_id="note:1", source_type="note", language="en", ingested_at=1)
    assert snapshot_document(store, doc, "<p>x</p>", fetched_at=1) is None


def test_snapshot_document_accepts_dicts(store):
    out = snapshot_document(store, {"url": "https://ex.com/d"}, "<p>dict doc</p>", fetched_at=5)
    assert out is not None
    assert store.has("https://ex.com/d")


def test_check_liveness_injectable():
    assert check_liveness("https://ex.com", http_head=lambda url: True) is True
    assert check_liveness("https://ex.com", http_head=lambda url: False) is False
    # A raising checker counts as dead, never as an error.
    def boom(url):
        raise ConnectionError("down")
    assert check_liveness("https://ex.com", http_head=boom) is False


def test_resolve_citation_live_paths(store):
    store.snapshot("https://ex.com/gone", "<p>archived body</p>", fetched_at=100)
    live = resolve_citation_live(store, "https://ex.com/gone", http_head=lambda u: True)
    assert live["source"] == "live" and live["cited"] is True
    dead = resolve_citation_live(store, "https://ex.com/gone", http_head=lambda u: False)
    assert dead["source"] == "archive" and dead["cited"] is True
    assert dead["text"] == "archived body"
    never = resolve_citation_live(store, "https://ex.com/never", http_head=lambda u: False)
    assert never["cited"] is False and never["source"] == "none"


def test_prune_keeps_each_urls_latest(store):
    store.snapshot("https://ex.com/a", "<p>v1</p>", fetched_at=100)
    store.snapshot("https://ex.com/a", "<p>v2</p>", fetched_at=200)
    store.snapshot("https://ex.com/b", "<p>only</p>", fetched_at=100)
    deleted = prune_snapshots(store, now_ms=10_000, max_age_ms=5_000)
    # Both old rows are past cutoff, but each URL's latest survives.
    assert deleted == 1
    assert store.latest("https://ex.com/a").text == "v2"
    assert store.latest("https://ex.com/b").text == "only"  # last copy never pruned


def test_prune_without_policy_is_noop(store):
    store.snapshot("https://ex.com/a", "<p>v1</p>", fetched_at=1)
    assert prune_snapshots(store, now_ms=10_000, max_age_ms=None) == 0
    assert store.has("https://ex.com/a")


def test_prune_keep_latest_false_drops_all_old(store):
    store.snapshot("https://ex.com/a", "<p>v1</p>", fetched_at=100)
    deleted = prune_snapshots(store, now_ms=10_000, max_age_ms=5_000, keep_latest=False)
    assert deleted == 1
    assert store.latest("https://ex.com/a") is None
