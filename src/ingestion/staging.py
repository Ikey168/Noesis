"""
Staging-and-merge write path, decoupled from the DuckDB single-writer lock (#898).

DuckDB allows a single writer, so ingestion historically required stopping the
API server before a harvest (``scrapy_integration.main()``: *"stop the API
server first — DuckDB allows a single writer"*). That blocks continuous or
scheduled ingestion while the API is serving reads.

This module breaks the coupling:

- :func:`open_staging` gives the harvest its **own** DuckDB file (a
  :class:`~src.ingestion.document_store.DocumentStore` over it), so a long
  harvest writes without ever touching the serving warehouse's writer lock;
- :func:`merge_staging` attaches that staging file to the main warehouse and
  ``INSERT``\\s only the rows not already present, in one short-lived writer
  window (a maintenance tick), so the serving process holds the lock for
  milliseconds instead of the whole harvest.

The merge is **idempotent**: it dedups by ``document_id`` and by
``(content_hash, source_type)`` against the main warehouse, so re-merging the
same staging adds nothing new — the same semantics :class:`DocumentStore`
applies within a single database, now applied across the staging boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.ingestion.document_store import DocumentStore

logger = logging.getLogger(__name__)

# Columns of the documents table, in schema order (mirrors DocumentStore._SCHEMA).
_COLUMNS = (
    "document_id", "source_type", "language", "ingested_at", "created_at",
    "source_id", "url", "canonical_url", "content_hash", "title", "content",
    "content_ref", "authors", "metadata",
)


@dataclass
class MergeSummary:
    """Outcome of one :func:`merge_staging` call."""

    staged: int = 0    # rows present in the staging warehouse
    merged: int = 0    # rows inserted into the main warehouse
    skipped: int = 0   # staged rows already present (deduped)

    def as_dict(self) -> dict:
        return {"staged": self.staged, "merged": self.merged, "skipped": self.skipped}


def open_staging(path: str) -> DocumentStore:
    """Open (creating if needed) a staging DuckDB at ``path`` as a DocumentStore.

    The returned store is an ordinary :class:`DocumentStore` — validated,
    deduped, idempotent — but backed by a standalone file, so writing to it
    never contends with the serving warehouse. Close its ``.conn`` before
    merging so the file can be attached read-only.
    """
    import duckdb

    return DocumentStore(duckdb.connect(path))


def merge_staging(
    main_conn: Any,
    staging_path: str,
    *,
    attach_name: str = "staging_merge",
) -> MergeSummary:
    """Merge the ``documents`` rows from a staging DuckDB file into the main warehouse.

    Attaches ``staging_path`` read-only, inserts only the rows whose
    ``document_id`` is absent *and* whose ``(content_hash, source_type)`` is not
    already present, then detaches. Runs in a single short writer window on
    ``main_conn``. Idempotent: re-merging the same staging inserts nothing.

    Returns a :class:`MergeSummary`. The staging file's connection must be
    closed before calling (DuckDB cannot attach a file held open elsewhere).
    """
    # Guarantee the main warehouse has the documents table before merging into it.
    DocumentStore(main_conn)

    main_conn.execute(f"ATTACH '{staging_path}' AS {attach_name} (READ_ONLY)")
    try:
        staged = main_conn.execute(
            f"SELECT COUNT(*) FROM {attach_name}.documents"
        ).fetchone()[0]

        cols = ", ".join(_COLUMNS)
        before = main_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        main_conn.execute(
            f"""
            INSERT INTO documents ({cols})
            SELECT {', '.join('s.' + c for c in _COLUMNS)}
            FROM {attach_name}.documents s
            WHERE s.document_id NOT IN (SELECT document_id FROM documents)
              AND NOT EXISTS (
                  SELECT 1 FROM documents m
                  WHERE m.content_hash IS NOT NULL
                    AND m.content_hash = s.content_hash
                    AND m.source_type = s.source_type
              )
            """
        )
        after = main_conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        main_conn.execute(f"DETACH {attach_name}")

    merged = after - before
    summary = MergeSummary(staged=staged, merged=merged, skipped=staged - merged)
    logger.info(
        "staging-merge: staged=%d merged=%d skipped=%d (from %s)",
        summary.staged, summary.merged, summary.skipped, staging_path,
    )
    return summary
