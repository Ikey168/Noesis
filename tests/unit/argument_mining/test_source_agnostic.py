"""Source-type-agnostic argument-mining reads (DS-D).

The evidence corpus loader, conflict/stance/position sweeps, and the actor batch
resolved documents through the news-only ``news_articles`` view, dropping
blog / paper / transcript / book / note documents. They now read through
``corpus_table``. These pin that a non-news document is included, and that the
resolver falls back to ``news_articles`` for legacy fixtures.
"""

from __future__ import annotations

import duckdb
import pytest

from services.ingest.common.document_model import Document
from src.argument_mining.evidence import _default_corpus
from src.database.news_articles_compat import corpus_table, ensure_corpus_documents_view
from src.ingestion.document_store import DocumentStore

_TS = 1_700_000_000_000


def _doc(doc_id, source_type, source, content):
    return Document(
        document_id=doc_id, source_type=source_type, language="en",
        ingested_at=_TS, created_at=_TS, url=f"https://ex.com/{doc_id}",
        title=f"{source_type} headline", content=content,
        source_id=source, metadata={"source": source, "category": "general"},
    )


@pytest.fixture
def wh():
    conn = duckdb.connect(":memory:")
    DocumentStore(conn).upsert([
        _doc("n1", "news", "Reuters", "The central bank held rates steady this week."),
        _doc("p1", "paper", "Nature", "The measured warming trend continued through the decade."),
    ])
    ensure_corpus_documents_view(conn)
    return conn


def test_resolver_prefers_corpus_documents(wh):
    assert corpus_table(wh) == "corpus_documents"


def test_evidence_corpus_includes_non_news(wh):
    entries = _default_corpus(wh, exclude_document_id="n1")
    doc_ids = {e[1] for e in entries}
    assert "p1" in doc_ids       # the paper document is in the retrieval corpus
    assert "n1" not in doc_ids   # the excluded document is absent


def test_falls_back_to_news_articles_without_corpus_view():
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, "
        "content VARCHAR, publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )
    assert corpus_table(conn) == "news_articles"
    conn.close()
