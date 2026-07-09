"""
Persisted summary sink, keyed by ``document_id``.

A document-level summary is a distinct enrichment (variable-length text, its own
method), so it gets its own table rather than a column on ``document_enrichments``.
Written by :func:`src.ingestion.summarize.summarize_documents`; read by the
summarization MCP tools and any consumer that wants a short-form of a document
without re-running a model. The connection is injected (offline-testable) and
``upsert`` is idempotent (last write wins per document).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_summaries (
    document_id TEXT PRIMARY KEY,
    summary     TEXT,
    method      TEXT,
    updated_at  BIGINT
)
"""


class SummaryStore:
    """Idempotent per-document store for short-form summaries."""

    def __init__(self, conn):
        self.conn = conn
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.execute(_SCHEMA)

    def upsert(
        self,
        document_id: str,
        summary: str,
        *,
        method: str,
        updated_at: Optional[int] = None,
    ) -> None:
        """Insert or replace the summary for ``document_id`` (last write wins)."""
        self.conn.execute(
            """
            INSERT INTO document_summaries (document_id, summary, method, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (document_id) DO UPDATE SET
                summary    = excluded.summary,
                method     = excluded.method,
                updated_at = excluded.updated_at
            """,
            [document_id, summary, method, updated_at],
        )

    def get(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Return ``{summary, method}`` for ``document_id``, or None if absent."""
        row = self.conn.execute(
            "SELECT summary, method FROM document_summaries WHERE document_id = ?",
            [document_id],
        ).fetchone()
        if row is None:
            return None
        return {"summary": row[0], "method": row[1]}

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM document_summaries").fetchone()[0]
