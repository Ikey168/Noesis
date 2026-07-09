"""
``news_articles`` as a compatibility view over the unified ``documents`` sink (#909).

The knowledge-engine pivot made ``documents`` (document-ingest-v1) the canonical
corpus, but ~40 consumers still read ``FROM news_articles``. Rather than rewrite
every query, ``news_articles`` becomes a **view** projecting the news documents
(``source_type='news'``) back into the legacy column shape, joined to the
``document_enrichments`` sink for sentiment. Readers are unchanged; the base of
truth is ``documents`` (+ ``document_enrichments``).

Because a view is not writable, the few paths that used to ``INSERT INTO
news_articles`` write through :func:`write_news_articles`, which maps the legacy
columns onto ``documents`` + ``document_enrichments`` (the inverse of the view).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional

from src.ingestion.document_store import DocumentStore
from src.ingestion.enrichment_store import EnrichmentStore

# Legacy news_articles columns, in order.
NEWS_ARTICLES_COLUMNS = (
    "id", "title", "url", "content", "publish_date",
    "source", "category", "sentiment_score", "sentiment_label",
)

# news_articles projected from documents (+ enrichments). created_at is epoch ms;
# source/category live in metadata; sentiment comes from the enrichment sink.
_VIEW_SQL = """
CREATE VIEW news_articles AS
SELECT
    d.document_id AS id,
    d.title       AS title,
    d.url         AS url,
    d.content     AS content,
    CASE WHEN d.created_at IS NULL THEN NULL
         ELSE CAST(to_timestamp(d.created_at / 1000) AS TIMESTAMP) END AS publish_date,
    COALESCE(d.source_id, json_extract_string(d.metadata, '$.source')) AS source,
    json_extract_string(d.metadata, '$.category') AS category,
    e.sentiment_score AS sentiment_score,
    e.sentiment_label AS sentiment_label
FROM documents d
LEFT JOIN document_enrichments e ON e.document_id = d.document_id
WHERE d.source_type = 'news'
"""


# The source-type-agnostic companion of the news_articles view: the same legacy
# column shape over *every* document (no source_type filter), plus source_type
# and the enrichment topics. OSINT resolves citations from this so a blog, paper
# or filing resolves to its source exactly like a news article does, instead of
# being mis-flagged uncited by the news-only view.
_CORPUS_VIEW_SQL = """
CREATE VIEW corpus_documents AS
SELECT
    d.document_id AS id,
    d.title       AS title,
    d.url         AS url,
    d.content     AS content,
    CASE WHEN d.created_at IS NULL THEN NULL
         ELSE CAST(to_timestamp(d.created_at / 1000) AS TIMESTAMP) END AS publish_date,
    COALESCE(d.source_id, json_extract_string(d.metadata, '$.source')) AS source,
    json_extract_string(d.metadata, '$.category') AS category,
    d.source_type     AS source_type,
    e.sentiment_score AS sentiment_score,
    e.sentiment_label AS sentiment_label,
    e.topics          AS topics
FROM documents d
LEFT JOIN document_enrichments e ON e.document_id = d.document_id
"""


def _news_articles_is_view(conn) -> bool:
    row = conn.execute(
        "SELECT table_type FROM information_schema.tables WHERE table_name = 'news_articles'"
    ).fetchone()
    return bool(row) and str(row[0]).upper() == "VIEW"


def corpus_table(conn) -> str:
    """The corpus table a *source-agnostic* reader should query.

    Prefers the ``corpus_documents`` view (every ``source_type``) so blog, paper,
    transcript, book and note documents are counted alongside news; falls back to
    the news-only ``news_articles`` view/table for legacy warehouses and test
    fixtures that only seed it. Always returns a name — defaulting to
    ``news_articles`` preserves the prior behaviour when neither view exists.
    """
    try:
        for name in ("corpus_documents", "news_articles"):
            row = conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
            ).fetchone()
            if row:
                return name
    except Exception:
        # A connection that cannot introspect (e.g. a white-box test double):
        # fall back to the legacy name rather than crashing the caller.
        pass
    return "news_articles"


def ensure_documents_schema(conn) -> None:
    """Ensure the base tables the view depends on exist."""
    DocumentStore(conn)     # documents (+ content_hash index)
    EnrichmentStore(conn)   # document_enrichments


def ensure_corpus_documents_view(conn) -> None:
    """Create the source-type-agnostic ``corpus_documents`` view if absent.

    Unlike ``news_articles`` (news-only), this projects every document into the
    legacy column shape, so OSINT and other source-agnostic readers resolve a
    document's source/citation regardless of its ``source_type``. A no-op if it
    already exists. ``corpus_documents`` is a new name, so it never collides with
    a legacy ``news_articles`` base table.
    """
    ensure_documents_schema(conn)
    exists = conn.execute(
        "SELECT table_type FROM information_schema.tables WHERE table_name = 'corpus_documents'"
    ).fetchone()
    if exists is None:
        conn.execute(_CORPUS_VIEW_SQL)


def ensure_news_articles_view(conn) -> None:
    """Create the ``news_articles`` view over ``documents`` if it is not present.

    A no-op if ``news_articles`` already exists as a view. If it exists as a base
    table (a legacy warehouse, or a test that created its own), it is left alone
    — callers that want the view over a legacy table must migrate it first (see
    :func:`migrate_news_articles_to_view`).

    The source-agnostic ``corpus_documents`` view is ensured alongside, so every
    warehouse-setup path that builds the news view also gets the corpus view.
    """
    ensure_documents_schema(conn)
    exists = conn.execute(
        "SELECT table_type FROM information_schema.tables WHERE table_name = 'news_articles'"
    ).fetchone()
    if exists is None:
        conn.execute(_VIEW_SQL)
    ensure_corpus_documents_view(conn)


def write_news_articles(conn, rows: Iterable[Dict[str, Any]]) -> int:
    """Write legacy ``news_articles``-shaped rows through to ``documents``.

    Each row is a dict with any subset of :data:`NEWS_ARTICLES_COLUMNS` (``id``
    required). ``source``/``category`` fold into ``documents.metadata``,
    ``publish_date`` into ``created_at`` (epoch ms), and sentiment into
    ``document_enrichments``. Idempotent by ``document_id``. Returns the number
    of documents written.
    """
    ensure_documents_schema(conn)
    written = 0
    for row in rows:
        doc_id = row.get("id")
        if not doc_id:
            continue
        metadata: Dict[str, Any] = {}
        if row.get("source") is not None:
            metadata["source"] = row["source"]
        if row.get("category") is not None:
            metadata["category"] = row["category"]
        pub = row.get("publish_date")
        conn.execute(
            """
            INSERT INTO documents
                (document_id, source_type, language, ingested_at, created_at,
                 source_id, url, title, content, metadata)
            VALUES (?, 'news', 'en', ?,
                    CASE WHEN ? IS NULL THEN NULL ELSE epoch_ms(CAST(? AS TIMESTAMP)) END,
                    ?, ?, ?, ?, ?)
            ON CONFLICT (document_id) DO NOTHING
            """,
            [
                doc_id, int(row.get("ingested_at") or 0),
                pub, pub,
                row.get("source"), row.get("url"),
                row.get("title"), row.get("content"),
                json.dumps(metadata),
            ],
        )
        if row.get("sentiment_score") is not None or row.get("sentiment_label") is not None:
            EnrichmentStore(conn).upsert(
                doc_id,
                sentiment_score=row.get("sentiment_score"),
                sentiment_label=row.get("sentiment_label"),
            )
        written += 1
    return written


def migrate_news_articles_to_view(conn) -> int:
    """Migrate a legacy ``news_articles`` base table into ``documents`` + the view.

    Copies every existing ``news_articles`` row into ``documents`` (+
    ``document_enrichments``) via :func:`write_news_articles`, drops the base
    table, and creates the view. Idempotent: when ``news_articles`` is already
    the view (or absent), this only ensures the view exists and returns 0.
    Returns the number of migrated rows.
    """
    row = conn.execute(
        "SELECT table_type FROM information_schema.tables WHERE table_name = 'news_articles'"
    ).fetchone()
    migrated = 0
    if row is not None and str(row[0]).upper() in ("BASE TABLE", "LOCAL TEMPORARY"):
        colnames = [
            c[0] for c in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'news_articles'"
            ).fetchall()
        ]
        legacy = conn.execute(
            f"SELECT {', '.join(colnames)} FROM news_articles"
        ).fetchall()
        dict_rows = [dict(zip(colnames, r)) for r in legacy]
        conn.execute("DROP TABLE news_articles")
        migrated = write_news_articles(conn, dict_rows)
    ensure_news_articles_view(conn)
    return migrated
