"""Unit tests for the document enrichment pass (#922). Offline, in-memory DuckDB."""

from __future__ import annotations

import duckdb
import pytest

from services.ingest.common.document_model import Document
from src.ingestion.document_store import DocumentStore
from src.ingestion.enrich import (
    default_analyzer,
    enrich_documents,
    keyword_topics,
    lexicon_sentiment,
)
from src.ingestion.enrichment_store import EnrichmentStore


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    DocumentStore(c)
    EnrichmentStore(c)
    return c


def _add(store, doc_id, content, title=""):
    store.upsert([Document(document_id=doc_id, source_type="news", language="en",
                           ingested_at=1_700_000_000_000, title=title,
                           content=content, url=f"https://ex.com/{doc_id}")])


# --------------------------------------------------------------------------- #
# analyzer
# --------------------------------------------------------------------------- #


def test_lexicon_sentiment_polarity():
    assert lexicon_sentiment("Markets surge as profits gain and growth beats")["sentiment_label"] == "positive"
    assert lexicon_sentiment("Losses mount as the crisis deepens and stocks plunge")["sentiment_label"] == "negative"
    assert lexicon_sentiment("The committee met on Tuesday")["sentiment_label"] == "neutral"


def test_lexicon_sentiment_score_range():
    s = lexicon_sentiment("gain gain loss")["sentiment_score"]
    assert -1.0 <= s <= 1.0


def test_keyword_topics_extracts_frequent_terms():
    topics = keyword_topics("budget budget parliament parliament parliament economy the a an")
    assert topics[0] == "parliament"  # most frequent, >4 chars, non-stopword
    assert "budget" in topics
    assert "the" not in topics  # stopword excluded


def test_default_analyzer_shape():
    r = default_analyzer({"title": "Growth surges", "content": "profits gain"})
    assert set(r) == {"sentiment_score", "sentiment_label", "topics"}
    assert isinstance(r["topics"], list)


# --------------------------------------------------------------------------- #
# enrich_documents
# --------------------------------------------------------------------------- #


def test_enriches_documents_and_persists(conn):
    store = DocumentStore(conn)
    _add(store, "d1", "Markets surge as profits gain", title="Boom")
    _add(store, "d2", "Losses mount amid the crisis", title="Bust")

    n = enrich_documents(conn)
    assert n == 2
    enr = EnrichmentStore(conn)
    assert enr.get("d1")["sentiment_label"] == "positive"
    assert enr.get("d2")["sentiment_label"] == "negative"
    assert isinstance(enr.get("d1")["topics"], list)


def test_is_idempotent_only_fills_gaps(conn):
    store = DocumentStore(conn)
    _add(store, "d1", "Markets surge")
    assert enrich_documents(conn) == 1
    # Re-running enriches nothing new (d1 already has an enrichment).
    assert enrich_documents(conn) == 0
    # A newly-added document is picked up on the next pass.
    _add(store, "d2", "Crisis deepens")
    assert enrich_documents(conn) == 1


def test_limit_caps_the_batch(conn):
    store = DocumentStore(conn)
    for i in range(5):
        _add(store, f"d{i}", f"content {i} gain")
    assert enrich_documents(conn, limit=2) == 2
    assert EnrichmentStore(conn).count() == 2


def test_pluggable_analyzer_is_used(conn):
    store = DocumentStore(conn)
    _add(store, "d1", "anything")

    def _fixed(_doc):
        return {"sentiment_score": 0.42, "sentiment_label": "custom", "topics": ["x"]}

    enrich_documents(conn, analyzer=_fixed)
    rec = EnrichmentStore(conn).get("d1")
    assert rec["sentiment_score"] == 0.42
    assert rec["sentiment_label"] == "custom"
    assert rec["topics"] == ["x"]
