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

from typing import Any, List, Optional

from src.database.news_articles_compat import ensure_corpus_documents_view
from src.ingestion.embedding_store import EmbeddingStore

# Documents longer than this are truncated before embedding (one vector per
# document; the head carries the lede/topic for a document-level match).
DEFAULT_TEXT_MAX_CHARS = 4000


def _default_provider():
    from services.embeddings.provider import get_embedding_provider

    return get_embedding_provider()


def embed_documents(
    conn,
    provider: Optional[Any] = None,
    limit: Optional[int] = None,
    text_max_chars: int = DEFAULT_TEXT_MAX_CHARS,
) -> int:
    """Embed documents that have no embedding yet; return how many were embedded.

    Reads ``corpus_documents`` LEFT JOIN ``document_embeddings`` for the
    un-embedded rows, embeds them with ``provider`` (default: the env-configured
    provider), and upserts the vectors into ``document_embeddings``.
    """
    provider = provider or _default_provider()
    ensure_corpus_documents_view(conn)   # documents + enrichments + the view
    store = EmbeddingStore(conn)

    query = (
        "SELECT d.id, d.title, d.content FROM corpus_documents d "
        "LEFT JOIN document_embeddings e ON e.document_id = d.id "
        "WHERE e.document_id IS NULL ORDER BY d.publish_date DESC NULLS LAST"
    )
    params: List[Any] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    if not rows:
        return 0

    ids = [r[0] for r in rows]
    texts = [f"{(r[1] or '')} {(r[2] or '')}".strip()[:text_max_chars] for r in rows]
    matrix = provider.embed_texts(texts)
    model, dim = provider.name(), provider.dim()

    for document_id, vec in zip(ids, matrix):
        store.upsert(document_id, list(vec), model=model, dim=dim)
    return len(ids)
