"""Source-type-agnostic analytics reads (DS-C).

anomalies / drift / lead_lag / narratives used to read the news-only
``news_articles`` view, silently dropping blog / paper / transcript / book / note
documents. They now read through ``corpus_table`` (the source-agnostic
``corpus_documents`` view when present). These pin that a non-news document is
counted, and that the resolver falls back to ``news_articles`` for legacy
fixtures.
"""

from __future__ import annotations

import duckdb
import pytest

from services.ingest.common.document_model import Document
from src.analytics import anomalies, lead_lag, narratives
from src.database.news_articles_compat import corpus_table, ensure_corpus_documents_view
from src.ingestion.document_store import DocumentStore

D1 = 1_700_000_000_000
D2 = D1 + 86_400_000  # +1 day


def _doc(doc_id, source_type, source, category, day_ms):
    return Document(
        document_id=doc_id, source_type=source_type, language="en",
        ingested_at=day_ms, created_at=day_ms, url=f"https://ex.com/{doc_id}",
        title=f"{category} headline", content="body text",
        source_id=source, metadata={"source": source, "category": category},
    )


@pytest.fixture
def wh():
    conn = duckdb.connect(":memory:")
    DocumentStore(conn).upsert([
        _doc("n1", "news", "Reuters", "Economy", D1),
        _doc("p1", "paper", "Nature", "Science", D1),   # non-news, unique category
        _doc("b1", "blog", "Field Notes", "Economy", D2),  # non-news, shared category
    ])
    ensure_corpus_documents_view(conn)
    return conn


def test_resolver_prefers_corpus_documents(wh):
    assert corpus_table(wh) == "corpus_documents"


def test_narratives_docs_include_non_news(wh):
    ids = {d["id"] for d in narratives._docs(wh, topic=None, days=None)}
    assert {"p1", "b1"} <= ids  # paper + blog surfaced, not just news


def test_anomaly_series_includes_a_non_news_only_category(wh):
    series = anomalies._series_by_topic(wh)
    # "Science" exists only on the paper document — invisible under news-only.
    assert "Science" in series
    assert "Economy" in series


def test_lead_lag_outlet_series_includes_a_non_news_source(wh):
    series = lead_lag._outlet_series(wh, topic="Economy", outlets=None)
    assert "Field Notes" in series  # the blog outlet
    assert "Reuters" in series


def test_falls_back_to_news_articles_without_the_corpus_view():
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, "
        "content VARCHAR, publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )
    assert corpus_table(conn) == "news_articles"
    conn.close()
