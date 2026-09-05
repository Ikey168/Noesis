"""
Document embedding pass over the unified corpus.

Reads documents that have no embedding yet (source-type-agnostic, via the
``corpus_documents`` view), embeds ``title + content`` with a pluggable
:class:`~services.embeddings.provider.EmbeddingProvider`, and persists one vector
per document into :class:`~src.ingestion.embedding_store.EmbeddingStore`. This is
the indexer that makes semantic search / near-duplicate detection / embedding
topic-modelling possible over the DuckDB corpus.

The provider is injected. In production it defaults to the env-configured
provider (``local`` sentence-transformers by default); tests and offline runs
pass the deterministic ``hashing`` provider, so the pass runs (and is
gate-tested) with no heavy dependencies. Idempotent: an already-embedded
document is skipped, so re-running only fills the gaps.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np

from src.database.news_articles_compat import ensure_corpus_documents_view
from src.ingestion.embedding_store import EmbeddingStore

# Documents longer than this are truncated before embedding (one vector per
# document; the head carries the lede/topic for a document-level match).
DEFAULT_TEXT_MAX_CHARS = 4000
DEFAULT_BATCH_SIZE = 32


def _validate_embedding_matrix(matrix: Any, *, expected_count: int, expected_dim: int) -> None:
    if not isinstance(matrix, np.ndarray) or matrix.ndim != 2:
        raise ValueError("Embedding provider output must be a two-dimensional matrix")
    if matrix.shape[0] != expected_count:
        raise ValueError(f"Embedding provider returned {matrix.shape[0]} vectors for {expected_count} texts")
    if matrix.shape[1] != expected_dim:
        raise ValueError(f"Embedding provider returned dimension {matrix.shape[1]}; expected {expected_dim}")
    try:
        finite = np.isfinite(matrix).all()
    except TypeError as exc:
        raise ValueError("Embedding provider returned non-numeric vector values") from exc
    if not finite:
        raise ValueError("Embedding provider returned non-finite vector values")


def _default_provider():
    from services.embeddings.provider import get_embedding_provider

    return get_embedding_provider()


def embed_documents(
    conn,
    provider: Optional[Any] = None,
    limit: Optional[int] = None,
    text_max_chars: int = DEFAULT_TEXT_MAX_CHARS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Embed documents that have no embedding yet; return how many were embedded.

    Reads ``corpus_documents`` LEFT JOIN ``document_embeddings`` for the
    un-embedded rows, embeds them with ``provider`` (default: the env-configured
    provider), validates each output matrix, and upserts the vectors into
    ``document_embeddings`` in bounded batches.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    provider = provider or _default_provider()
    ensure_corpus_documents_view(conn)   # documents + enrichments + the view
    store = EmbeddingStore(conn)

    query = (
        "SELECT d.id, d.title, d.content FROM corpus_documents d "
        "LEFT JOIN document_embeddings e ON e.document_id = d.id "
        "WHERE e.document_id IS NULL ORDER BY d.publish_date DESC NULLS LAST LIMIT ?"
    )
    model, dim = provider.name(), provider.dim()
    remaining = limit
    persisted = 0

    while remaining is None or remaining > 0:
        current_batch_size = batch_size if remaining is None else min(batch_size, remaining)
        rows = conn.execute(query, [current_batch_size]).fetchall()
        if not rows:
            break

        ids = [row[0] for row in rows]
        texts = [f"{(row[1] or '')} {(row[2] or '')}".strip()[:text_max_chars] for row in rows]
        matrix = provider.embed_texts(texts)
        _validate_embedding_matrix(matrix, expected_count=len(ids), expected_dim=dim)

        for document_id, vector in zip(ids, matrix):
            store.upsert(document_id, list(vector), model=model, dim=dim)

        persisted += len(ids)
        if remaining is not None:
            remaining -= len(ids)

    return persisted
