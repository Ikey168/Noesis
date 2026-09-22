"""
Persisted embedding sink, keyed by ``document_id``.

The vector infrastructure the repo already has (``services/rag`` pgvector,
Qdrant, the Snowflake ``article_embeddings`` path) targets other stores; nothing
embedded the canonical DuckDB ``documents`` corpus. ``EmbeddingStore`` is that
missing sink: one dense vector per document, so semantic search, near-duplicate
detection, and embedding-based topic modelling can run over the same corpus the
rest of the engine reads.

The vector is stored as a JSON array of floats (portable across DuckDB builds,
no vector extension required); the embedding ``model`` and ``dim`` are recorded
alongside so a reader can tell which space a vector lives in. The connection is
injected, so it is offline-testable, and ``upsert`` is idempotent (last write
wins per document).
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_embeddings (
    document_id TEXT PRIMARY KEY,
    model       TEXT,
    dim         INTEGER,
    vector      TEXT,   -- JSON array of floats
    updated_at  BIGINT
)
"""


class EmbeddingStore:
    """Idempotent per-document store for dense embedding vectors."""

    def __init__(self, conn):
        self.conn = conn
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.execute(_SCHEMA)

    def upsert(
        self,
        document_id: str,
        vector: Sequence[float],
        *,
        model: str,
        dim: Optional[int] = None,
        updated_at: Optional[int] = None,
    ) -> None:
        """Insert or replace the embedding for ``document_id`` (last write wins)."""
        vec = [float(x) for x in vector]
        declared_dim = int(dim if dim is not None else len(vec))
        if not model or declared_dim <= 0 or len(vec) != declared_dim or not all(math.isfinite(x) for x in vec):
            raise ValueError("embedding must match a positive dimension and contain finite values")
        self.conn.execute(
            """
            INSERT INTO document_embeddings (document_id, model, dim, vector, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (document_id) DO UPDATE SET
                model      = excluded.model,
                dim        = excluded.dim,
                vector     = excluded.vector,
                updated_at = excluded.updated_at
            """,
            [document_id, model, declared_dim,
             json.dumps(vec), updated_at],
        )

    def get(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Return ``{model, dim, vector}`` for ``document_id``, or None if absent."""
        row = self.conn.execute(
            "SELECT model, dim, vector FROM document_embeddings WHERE document_id = ?",
            [document_id],
        ).fetchone()
        if row is None:
            return None
        return {"model": row[0], "dim": row[1], "vector": json.loads(row[2]) if row[2] else []}

    def vectors(self, model: Optional[str] = None) -> List[Tuple[str, List[float]]]:
        """All ``(document_id, vector)`` pairs, optionally filtered to one model."""
        if model is not None:
            rows = self.conn.execute(
                "SELECT document_id, vector FROM document_embeddings WHERE model = ?",
                [model],
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT document_id, vector FROM document_embeddings"
            ).fetchall()
        return [(r[0], json.loads(r[1]) if r[1] else []) for r in rows]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM document_embeddings").fetchone()[0]
