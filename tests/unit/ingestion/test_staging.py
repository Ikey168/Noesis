"""Unit tests for the staging + merge write path (#898).

Offline: two temp DuckDB files (a serving warehouse and a staging warehouse).
Proves a harvest can write to staging while a reader holds the main DB, and
that merging staging into main is idempotent and content-deduped.
"""

from __future__ import annotations

import duckdb
import pytest

from services.ingest.common.document_model import Document
from src.ingestion.document_store import DocumentStore
from src.ingestion.staging import MergeSummary, merge_staging, open_staging


def _doc(doc_id: str, *, content: str = "Body text.", source_type: str = "news",
         url: str | None = None) -> Document:
    return Document(
        document_id=doc_id,
        source_type=source_type,
        language="en",
        ingested_at=1_700_000_000_000,
        url=url or f"https://ex.com/{doc_id}",
        content=content,
    )


def _paths(tmp_path):
    return str(tmp_path / "main.duckdb"), str(tmp_path / "staging.duckdb")


# --------------------------------------------------------------------------- #
# Decoupling: write to staging while a reader holds main
# --------------------------------------------------------------------------- #


def test_harvest_writes_to_staging_while_reader_holds_main(tmp_path):
    main_path, stg_path = _paths(tmp_path)

    # A reader holds the main warehouse open (as the serving API would).
    main = duckdb.connect(main_path)
    DocumentStore(main)
    assert main.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0

    # The harvest writes to a *separate* staging file — no lock contention.
    staging = open_staging(stg_path)
    summary = staging.upsert([_doc("d1"), _doc("d2", content="Other.")])
    assert summary.inserted == 2
    assert staging.count() == 2
    staging.conn.close()

    # The reader is unaffected and still sees an empty main warehouse.
    assert main.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #


def test_merge_moves_staged_rows_into_main(tmp_path):
    main_path, stg_path = _paths(tmp_path)
    main = duckdb.connect(main_path)

    staging = open_staging(stg_path)
    staging.upsert([_doc("d1"), _doc("d2", content="Second.")])
    staging.conn.close()

    summary = merge_staging(main, stg_path)
    assert summary.staged == 2
    assert summary.merged == 2
    assert summary.skipped == 0
    assert main.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    # The actual documents made it across, not just the count.
    ids = {r[0] for r in main.execute("SELECT document_id FROM documents").fetchall()}
    assert ids == {"d1", "d2"}


def test_merge_into_fresh_main_autocreates_schema(tmp_path):
    main_path, stg_path = _paths(tmp_path)
    staging = open_staging(stg_path)
    staging.upsert([_doc("d1")])
    staging.conn.close()

    # main has never had the documents table — merge must create it.
    main = duckdb.connect(main_path)
    summary = merge_staging(main, stg_path)
    assert summary.merged == 1


def test_merge_is_idempotent(tmp_path):
    main_path, stg_path = _paths(tmp_path)
    main = duckdb.connect(main_path)

    staging = open_staging(stg_path)
    staging.upsert([_doc("d1"), _doc("d2", content="Second.")])
    staging.conn.close()

    first = merge_staging(main, stg_path)
    second = merge_staging(main, stg_path)
    assert first.merged == 2
    assert second.merged == 0
    assert second.skipped == 2
    assert main.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2


def test_merge_skips_content_duplicates_already_in_main(tmp_path):
    main_path, stg_path = _paths(tmp_path)
    main = duckdb.connect(main_path)

    body = "Parliament approved the budget after a marathon debate."
    # A row already sits in main with this body.
    DocumentStore(main).upsert([_doc("main-1", content=body, url="https://a.com/x")])

    # Staging has the same body under a *different* id and URL (syndicated).
    staging = open_staging(stg_path)
    staging.upsert([
        _doc("stg-dup", content=body, url="https://b.com/y"),
        _doc("stg-new", content="A genuinely new story."),
    ])
    staging.conn.close()

    summary = merge_staging(main, stg_path)
    assert summary.staged == 2
    assert summary.merged == 1   # only the new story crosses over
    assert summary.skipped == 1
    ids = {r[0] for r in main.execute("SELECT document_id FROM documents").fetchall()}
    assert ids == {"main-1", "stg-new"}


def test_merge_accumulates_across_multiple_staging_files(tmp_path):
    main_path = str(tmp_path / "main.duckdb")
    main = duckdb.connect(main_path)

    for n in range(3):
        stg_path = str(tmp_path / f"staging-{n}.duckdb")
        staging = open_staging(stg_path)
        staging.upsert([_doc(f"batch{n}-a", content=f"A{n}"),
                        _doc(f"batch{n}-b", content=f"B{n}")])
        staging.conn.close()
        merge_staging(main, stg_path)

    assert main.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 6


def test_merge_empty_staging_is_a_noop(tmp_path):
    main_path, stg_path = _paths(tmp_path)
    main = duckdb.connect(main_path)
    staging = open_staging(stg_path)  # created, never written
    staging.conn.close()

    summary = merge_staging(main, stg_path)
    assert summary.as_dict() == {"staged": 0, "merged": 0, "skipped": 0}


def test_merge_summary_counts_reconcile(tmp_path):
    main_path, stg_path = _paths(tmp_path)
    main = duckdb.connect(main_path)
    DocumentStore(main).upsert([_doc("shared", content="Shared body.")])

    staging = open_staging(stg_path)
    staging.upsert([
        _doc("shared", content="Shared body."),   # dup id + content
        _doc("fresh1", content="Fresh one."),
        _doc("fresh2", content="Fresh two."),
    ])
    staging.conn.close()

    summary = merge_staging(main, stg_path)
    d = summary.as_dict()
    assert d["staged"] == d["merged"] + d["skipped"]
    assert d == {"staged": 3, "merged": 2, "skipped": 1}


def test_merge_summary_dataclass_defaults():
    s = MergeSummary()
    assert s.as_dict() == {"staged": 0, "merged": 0, "skipped": 0}
