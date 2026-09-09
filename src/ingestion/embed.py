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
pass the deterministic ``hashing`` provider, so the pass runs without a remote
model API. Matching content/configuration receipts are skipped; corrections and
model/configuration changes are processed again in bounded atomic batches.
"""

from __future__ import annotations

import math
from itertools import islice
from typing import Any

from src.database.news_articles_compat import ensure_corpus_documents_view
from src.ingestion.embedding_store import EmbeddingStore
from src.ingestion.processing_versions import (
    ProcessingVersions,
    configuration_hash,
    document_input_hash,
)

# Documents longer than this are truncated before embedding (one vector per
# document; the head carries the lede/topic for a document-level match).
DEFAULT_TEXT_MAX_CHARS = 4000
DEFAULT_BATCH_SIZE = 32


def _validate_embedding_matrix(
    matrix: Any, *, expected_count: int, expected_dim: int
) -> list[list[float]]:
    """Validate a complete bounded batch before any vector is persisted.

    Accept ndarray and iterable providers while consuming at most one extra
    row/coordinate to detect oversized output without exhausting an iterator.
    """
    if getattr(matrix, "ndim", 2) != 2:
        raise ValueError("Embedding provider output must be a two-dimensional matrix")
    try:
        rows = list(islice(iter(matrix), expected_count + 1))
    except TypeError as exc:
        raise ValueError(
            "Embedding provider output must be a two-dimensional matrix"
        ) from exc
    if len(rows) != expected_count:
        raise ValueError(
            f"Embedding provider returned {len(rows)} vectors for {expected_count} texts"
        )
    vectors = []
    for row in rows:
        if isinstance(row, (str, bytes, dict)):
            raise ValueError(  # noqa: TRY004 - provider validation consistently raises ValueError
                "Embedding provider output must be a two-dimensional matrix"
            )
        try:
            values = list(islice(iter(row), expected_dim + 1))
        except TypeError as exc:
            raise ValueError(
                "Embedding provider output must be a two-dimensional matrix"
            ) from exc
        if len(values) != expected_dim:
            raise ValueError(
                f"Embedding provider returned dimension {len(values)}; expected {expected_dim}"
            )
        try:
            vector = [float(value) for value in values]
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "Embedding provider returned non-numeric vector values"
            ) from exc
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("Embedding provider returned non-finite vector values")
        vectors.append(vector)
    return vectors


def _default_provider():
    from services.embeddings.provider import get_embedding_provider

    return get_embedding_provider()


def embed_documents(
    conn,
    provider: Any | None = None,
    limit: int | None = None,
    text_max_chars: int = DEFAULT_TEXT_MAX_CHARS,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batch_chars: int = 256_000,
    configuration: dict | None = None,
) -> int:
    """Refresh stale vectors in bounded, validated, independently atomic batches.

    Provider configuration must include any revision/tokenizer settings not
    already represented by its model name. The direct document projection
    retains its explicit text_max_chars setting; chunk retrieval is separate.
    Returns the number actually persisted, not the requested provider count.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if batch_size > 1000 or not 1 <= text_max_chars <= max_batch_chars:
        raise ValueError("invalid embedding batch/text bounds")
    if limit is not None and limit < 0:
        raise ValueError("embedding limit must be nonnegative")
    provider = provider or _default_provider()
    ensure_corpus_documents_view(conn)  # documents + enrichments + the view
    store = EmbeddingStore(conn)
    versions = ProcessingVersions(conn)
    model, dim = provider.name(), provider.dim()
    if not model or dim <= 0:
        raise ValueError("embedding provider must declare model and positive dimension")
    config_hash = configuration_hash(
        {
            "model": model,
            "dim": dim,
            "text_max_chars": text_max_chars,
            "configuration": configuration or {},
        }
    )
    input_sql = document_input_hash()
    batch_cap = min(batch_size, max_batch_chars // text_max_chars)
    after = None
    persisted = attempted = 0
    while limit is None or attempted < limit:
        take = min(batch_cap, limit - attempted) if limit is not None else batch_cap
        rows = conn.execute(
            f"SELECT d.document_id, substr(coalesce(d.title,''),1,?), "
            f"substr(coalesce(d.content,''),1,?), {input_sql} FROM documents d "
            "LEFT JOIN document_embeddings e ON e.document_id=d.document_id "
            "LEFT JOIN document_processing_versions p ON p.document_id=d.document_id AND p.stage='embedding' "
            f"WHERE (? IS NULL OR d.document_id>?) AND (e.document_id IS NULL "
            f"OR p.input_hash IS DISTINCT FROM {input_sql} OR p.configuration_hash IS DISTINCT FROM ? "
            "OR e.model IS DISTINCT FROM ? OR e.dim IS DISTINCT FROM ?) ORDER BY d.document_id LIMIT ?",
            [
                text_max_chars,
                text_max_chars,
                after,
                after,
                config_hash,
                model,
                dim,
                take,
            ],
        ).fetchall()
        if not rows:
            break
        texts = [f"{r[1]} {r[2]}".strip()[:text_max_chars] for r in rows]
        vectors = _validate_embedding_matrix(
            provider.embed_texts(texts), expected_count=len(rows), expected_dim=dim
        )
        conn.execute("BEGIN")
        written = 0
        try:
            for (document_id, _, _, input_hash), vector in zip(rows, vectors):
                current = conn.execute(
                    f"SELECT {input_sql} FROM documents d WHERE document_id=?",
                    [document_id],
                ).fetchone()
                if current is None or current[0] != input_hash:
                    continue
                store.upsert(document_id, vector, model=model, dim=dim)
                versions.record(document_id, "embedding", input_hash, config_hash)
                written += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        persisted += written
        attempted += len(rows)
        after = rows[-1][0]
    return persisted
