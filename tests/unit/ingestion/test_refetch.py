"""Unit tests for the staged re-fetch scheduler (#824) — offline."""

from __future__ import annotations

import pytest

from src.ingestion.corrections import record_revision, revision_history
from src.ingestion.refetch import DEFAULT_STAGES_MS, due_documents, run_refetch
from src.ingestion.snapshots import SnapshotStore

_DAY = 24 * 60 * 60 * 1000


@pytest.fixture()
def conn():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE documents (document_id TEXT, url TEXT, ingested_at BIGINT)")
    return c


def _add(conn, doc_id, url, ingested_at):
    conn.execute("INSERT INTO documents VALUES (?, ?, ?)", [doc_id, url, ingested_at])


def test_due_after_first_stage(conn):
    _add(conn, "news:1", "https://a.com/x", ingested_at=0)
    _add(conn, "news:2", "https://a.com/y", ingested_at=0)
    # Half a day in: nothing due yet.
    assert due_documents(conn, now_ms=_DAY // 2) == []
    # Two days in: both due for stage 0 (the 1d stage).
    due = due_documents(conn, now_ms=2 * _DAY)
    assert [(d["document_id"], d["stage"]) for d in due] == [("news:1", 0), ("news:2", 0)]


def test_completed_stage_not_redue(conn):
    _add(conn, "news:1", "https://a.com/x", ingested_at=0)
    record_revision(conn, "news:1", "original body of the article", fetched_at=0)  # rev 0 baseline
    # A stage-0 re-fetch happened at day 2 (content changed -> revision 1).
    record_revision(conn, "news:1", "a substantively different article body", fetched_at=2 * _DAY)
    # Day 3: stage 0 satisfied, stage 1 (7d) not yet reached -> nothing due.
    assert due_documents(conn, now_ms=3 * _DAY) == []
    # Day 8: stage 1 is now due.
    due = due_documents(conn, now_ms=8 * _DAY)
    assert [(d["document_id"], d["stage"]) for d in due] == [("news:1", 1)]


def test_urlless_documents_never_due(conn):
    _add(conn, "note:1", None, ingested_at=0)
    assert due_documents(conn, now_ms=10 * _DAY) == []


def test_run_refetch_records_changes_and_summary(conn):
    _add(conn, "news:1", "https://a.com/x", ingested_at=0)
    record_revision(conn, "news:1", "The mayor won 60% of the vote in the election.", fetched_at=0)
    fetched = {"https://a.com/x": "The mayor won 52% of the vote in the election."}
    summary = run_refetch(conn, lambda url: fetched[url], now_ms=2 * _DAY)
    assert summary["due"] == 1 and summary["checked"] == 1
    assert summary["changed"] == 1
    assert summary["by_class"] == {"silent_substantive": 1}
    assert len(revision_history(conn, "news:1")) == 2


def test_run_refetch_unchanged_content(conn):
    _add(conn, "news:1", "https://a.com/x", ingested_at=0)
    record_revision(conn, "news:1", "Same body.", fetched_at=0)
    summary = run_refetch(conn, lambda url: "Same body.", now_ms=2 * _DAY)
    assert summary["changed"] == 0
    assert summary["by_class"] == {"unchanged": 1}


def test_fetch_failure_skips_not_aborts(conn):
    _add(conn, "news:1", "https://a.com/x", ingested_at=0)
    _add(conn, "news:2", "https://b.com/y", ingested_at=0)
    record_revision(conn, "news:2", "original b body here", fetched_at=0)

    def fetcher(url):
        if "a.com" in url:
            raise ConnectionError("down")
        return "a very different b body entirely now"

    summary = run_refetch(conn, fetcher, now_ms=2 * _DAY)
    assert summary["skipped"] == 1
    assert summary["checked"] == 1  # b.com still processed


def test_per_domain_cap(conn):
    for i in range(8):
        _add(conn, f"news:{i}", f"https://a.com/{i}", ingested_at=0)
    summary = run_refetch(conn, lambda url: "body", now_ms=2 * _DAY, per_domain=3)
    assert summary["checked"] == 3
    assert summary["skipped"] == 5


def test_shared_fetch_pass_snapshots(conn):
    _add(conn, "news:1", "https://a.com/x", ingested_at=0)
    store = SnapshotStore(conn)
    run_refetch(conn, lambda url: "<p>fresh body</p>", now_ms=2 * _DAY, snapshot_store=store)
    snap = store.latest("https://a.com/x")
    assert snap is not None and snap.text == "fresh body"  # one fetch served both (#825)


def test_default_stages_shape():
    assert DEFAULT_STAGES_MS == (1 * _DAY, 7 * _DAY, 30 * _DAY)
