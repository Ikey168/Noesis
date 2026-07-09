"""Unit tests for archive-anchored citations (#790)."""

from __future__ import annotations

import pytest

from src.ingestion.snapshots import SnapshotStore, extract_text, resolve_citation


@pytest.fixture()
def store():
    duckdb = pytest.importorskip("duckdb")
    return SnapshotStore(duckdb.connect(":memory:"))


def test_extract_text_drops_markup_and_scripts():
    html = "<html><head><style>.x{}</style></head><body><script>evil()</script><p>Hello <b>world</b></p></body></html>"
    assert extract_text(html) == "Hello world"


def test_snapshot_and_latest(store):
    store.snapshot("https://ex.com/a", "<p>original text</p>", fetched_at=100)
    snap = store.latest("https://ex.com/a")
    assert snap is not None
    assert snap.text == "original text"
    assert snap.fetched_at == 100


def test_revisions_retained(store):
    store.snapshot("https://ex.com/a", "<p>v1</p>", fetched_at=100)
    store.snapshot("https://ex.com/a", "<p>v2 revised</p>", fetched_at=200)
    assert len(store.snapshots("https://ex.com/a")) == 2
    # latest() returns the newest.
    assert store.latest("https://ex.com/a").text == "v2 revised"


def test_snapshot_idempotent_per_fetch_time(store):
    store.snapshot("https://ex.com/a", "<p>x</p>", fetched_at=100)
    store.snapshot("https://ex.com/a", "<p>x</p>", fetched_at=100)
    assert len(store.snapshots("https://ex.com/a")) == 1


def test_resolve_live_link(store):
    store.snapshot("https://ex.com/a", "<p>text</p>", fetched_at=100)
    res = resolve_citation(store, "https://ex.com/a", live_ok=True)
    assert res["cited"] is True
    assert res["source"] == "live"
    assert res["archived"] is True  # a snapshot also exists


def test_resolve_dead_link_falls_back_to_archive(store):
    store.snapshot("https://ex.com/gone", "<p>archived body</p>", fetched_at=100)
    res = resolve_citation(store, "https://ex.com/gone", live_ok=False)
    assert res["cited"] is True  # survives link rot
    assert res["source"] == "archive"
    assert res["text"] == "archived body"
    assert res["fetched_at"] == 100


def test_resolve_dead_link_no_snapshot_is_flagged(store):
    res = resolve_citation(store, "https://ex.com/never", live_ok=False)
    assert res["cited"] is False  # the flagged uncited state
    assert res["source"] == "none"


def test_has_and_persistence(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    path = str(tmp_path / "snaps.duckdb")
    store = SnapshotStore(duckdb.connect(path))
    store.snapshot("https://ex.com/a", "<p>persist me</p>", fetched_at=100)
    store._conn.close()
    reopened = SnapshotStore(duckdb.connect(path))
    assert reopened.has("https://ex.com/a")
    assert reopened.latest("https://ex.com/a").text == "persist me"
