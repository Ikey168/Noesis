"""Source-type-agnostic citation resolution for OSINT (OS-C).

Before OS-C the OSINT tools resolved citations only through the news-only
``news_articles`` view, so a blog / paper / filing document was mis-flagged
uncited even though it is a first-class corpus document. These pin the
source-agnostic ``corpus_documents`` resolution and the projected enrichment
topics.
"""

from __future__ import annotations

import duckdb
import pytest

from services.ingest.common.document_model import Document
from src.database.news_articles_compat import ensure_corpus_documents_view
from src.ingestion.document_store import DocumentStore
from src.ingestion.enrichment_store import EnrichmentStore
from src.osint import common, evidence


def _doc(doc_id: str, source_type: str, source: str) -> Document:
    return Document(
        document_id=doc_id,
        source_type=source_type,
        language="en",
        ingested_at=1_700_000_000_000,
        created_at=1_700_000_000_000,
        url=f"https://ex.com/{doc_id}",
        title=f"{source_type} document",
        content="Body.",
        source_id=source,
        metadata={"source": source, "category": "general"},
    )


@pytest.fixture
def wh():
    conn = duckdb.connect(":memory:")
    store = DocumentStore(conn)
    enr = EnrichmentStore(conn)
    store.upsert([
        _doc("n1", "news", "Reuters"),
        _doc("p1", "paper", "Nature"),
        _doc("b1", "blog", "Field Notes"),
    ])
    enr.upsert("p1", sentiment_score=0.2, sentiment_label="positive",
               topics=["physics", "energy"])
    conn.execute(
        "CREATE TABLE argument_claims (claim_id VARCHAR, claim_text VARCHAR, "
        "document_id VARCHAR, source_type VARCHAR, confidence DOUBLE, factcheck_verdict VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO argument_claims (claim_id, claim_text, document_id, source_type) "
        "VALUES (?, ?, ?, ?)",
        [
            ("kn", "A news claim.", "n1", "news"),
            ("kp", "A paper claim.", "p1", "paper"),
            ("kb", "A blog claim.", "b1", "blog"),
        ],
    )
    ensure_corpus_documents_view(conn)
    return conn


def test_citation_table_prefers_the_source_agnostic_view(wh):
    assert common.citation_table(wh) == "corpus_documents"


def test_non_news_claim_resolves_to_its_source(wh):
    # The paper and blog claims resolve to their outlet — not mis-flagged uncited.
    srcs = common.claim_sources(wh, ["kn", "kp", "kb"])
    assert srcs["kp"]["source"] == "Nature" and srcs["kp"]["resolved"] is True
    assert srcs["kb"]["source"] == "Field Notes" and srcs["kb"]["resolved"] is True
    assert srcs["kn"]["source"] == "Reuters" and srcs["kn"]["resolved"] is True


def test_document_citations_project_enrichment_topics(wh):
    cites = evidence.document_citations(wh, ["p1", "n1"])
    assert cites["p1"]["cited"] is True
    assert cites["p1"]["topics"] == ["physics", "energy"]
    # A document without enrichment topics simply carries none.
    assert "topics" not in cites["n1"] or cites["n1"].get("topics") in (None, [])


def test_reliability_track_record_counts_non_news_sources(wh):
    from src.osint import source_reliability

    out = source_reliability(wh, "Nature")
    # Nature is a paper source; its track record is visible source-agnostically.
    assert out["track_record"]["documents"] == 1
    assert out["found"] is True


def test_fallback_to_news_articles_when_no_corpus_view():
    # A legacy fixture that only seeds news_articles still resolves (fallback).
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, "
        "content VARCHAR, publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )
    assert common.citation_table(conn) == "news_articles"
    conn.close()
