"""Unit tests for embedding-based topic modelling. Offline via the hashing
embedding backend (lexical vectors cluster same-vocabulary documents)."""

from __future__ import annotations

import duckdb
import pytest

from services.embeddings.provider import EmbeddingProvider
from services.ingest.common.document_model import Document
from src.analytics.topics import model_topics
from src.ingestion.document_store import DocumentStore
from src.ingestion.embed import embed_documents


@pytest.fixture
def provider():
    return EmbeddingProvider(provider="hashing")


def _doc(doc_id, content):
    return Document(document_id=doc_id, source_type="news", language="en",
                    ingested_at=1_700_000_000_000, created_at=1_700_000_000_000,
                    url=f"https://ex.com/{doc_id}", title=doc_id, content=content,
                    source_id="Wire", metadata={"source": "Wire"})


@pytest.fixture
def wh(provider):
    conn = duckdb.connect(":memory:")
    DocumentStore(conn).upsert([
        _doc("e1", "inflation cooled as the economy grew and interest rates held steady"),
        _doc("e2", "the economy grew while inflation cooled and interest rates held"),
        _doc("e3", "interest rates and inflation shaped the economy this quarter"),
        _doc("s1", "the football team won the championship final in overtime"),
        _doc("s2", "the championship final was won by the football team in overtime"),
    ])
    embed_documents(conn, provider=provider)
    return conn


def test_clusters_documents_into_topics(wh):
    out = model_topics(wh, min_similarity=0.25, min_cluster_size=2)
    assert out["count"] >= 2
    clusters = [set(t["document_ids"]) for t in out["topics"]]
    econ = next((c for c in clusters if "e1" in c), set())
    sport = next((c for c in clusters if "s1" in c), set())
    assert {"e1", "e2", "e3"} <= econ      # economy documents cluster together
    assert {"s1", "s2"} <= sport            # sports documents cluster together
    assert econ.isdisjoint(sport)           # and are separate topics


def test_topics_are_labelled_with_salient_terms(wh):
    out = model_topics(wh, min_similarity=0.25, min_cluster_size=2)
    econ = next(t for t in out["topics"] if "e1" in t["document_ids"])
    # The economy cluster's salient terms include its shared vocabulary.
    assert any(term in {"inflation", "economy", "rates", "interest"} for term in econ["terms"])
    assert econ["label"]


def test_empty_without_embeddings():
    conn = duckdb.connect(":memory:")
    DocumentStore(conn)
    out = model_topics(conn)
    assert out["count"] == 0 and "note" in out


def test_min_cluster_size_filters_singletons(wh):
    # A very high threshold makes every document its own singleton -> no topics
    # survive min_cluster_size=2.
    out = model_topics(wh, min_similarity=0.999, min_cluster_size=2)
    assert out["count"] == 0
