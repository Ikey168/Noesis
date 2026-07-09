"""
Persisted enrichment sink, keyed by ``document_id`` (#908).

Enrichments were deliberately split out of the core document record —
``DocumentEnrichments`` (sentiment, topics) in
:mod:`services.ingest.common.document_model` *extracts* them, but nothing
persisted them: the ``documents`` table has no sentiment/topic columns, and no
enrichment table existed. So sentiment/topic signals lived only on the legacy
``news_articles`` corpus, and would vanish the moment consumers moved off it.

``EnrichmentStore`` is that missing sink. It keeps the core ``documents`` record
enrichment-free — enrichments remain one analyzer's output among many — while
giving downstream analyzers a place to write and readers/joins a place to pull
sentiment and topics back by ``document_id``. The DuckDB connection is injected,
so it is offline-testable against an in-memory database, and ``upsert`` is
idempotent (last write wins per document).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_enrichments (
    document_id     TEXT PRIMARY KEY,
    sentiment_score DOUBLE,
    sentiment_label TEXT,
    topics          TEXT,   -- JSON array
    updated_at      BIGINT
)
"""


class EnrichmentStore:
    """Idempotent per-document store for sentiment/topic enrichments."""

    def __init__(self, conn):
        self.conn = conn
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.execute(_SCHEMA)

    def upsert(
        self,
        document_id: str,
        *,
        sentiment_score: Optional[float] = None,
        sentiment_label: Optional[str] = None,
        topics: Optional[List[str]] = None,
        updated_at: Optional[int] = None,
    ) -> None:
        """Insert or replace the enrichments for ``document_id`` (last write wins)."""
        self.conn.execute(
            """
            INSERT INTO document_enrichments
                (document_id, sentiment_score, sentiment_label, topics, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (document_id) DO UPDATE SET
                sentiment_score = excluded.sentiment_score,
                sentiment_label = excluded.sentiment_label,
                topics          = excluded.topics,
                updated_at      = excluded.updated_at
            """,
            [
                document_id,
                sentiment_score,
                sentiment_label,
                json.dumps(list(topics)) if topics is not None else None,
                updated_at,
            ],
        )

    def upsert_enrichments(
        self, document_id: str, enrichments: Dict[str, Any], updated_at: Optional[int] = None
    ) -> None:
        """Upsert from a ``DocumentEnrichments``-style dict (sentiment_score/topics)."""
        self.upsert(
            document_id,
            sentiment_score=enrichments.get("sentiment_score"),
            sentiment_label=enrichments.get("sentiment_label"),
            topics=enrichments.get("topics"),
            updated_at=updated_at,
        )

    def get(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Return the enrichments for ``document_id``, or None if absent."""
        row = self.conn.execute(
            "SELECT sentiment_score, sentiment_label, topics, updated_at "
            "FROM document_enrichments WHERE document_id = ?",
            [document_id],
        ).fetchone()
        if row is None:
            return None
        return {
            "sentiment_score": row[0],
            "sentiment_label": row[1],
            "topics": json.loads(row[2]) if row[2] else [],
            "updated_at": row[3],
        }

    def count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM document_enrichments"
        ).fetchone()[0]
