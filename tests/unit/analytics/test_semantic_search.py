"""Unit tests for semantic search over the document embedding sink. Offline via
the deterministic hashing backend (lexical similarity is enough to pin ranking).
"""

from __future__ import annotations

import duckdb
import pytest

from services.embeddings.provider import EmbeddingProvider
from services.ingest.common.document_model import Document
from src.analytics.semantic_search import near_duplicates, semantic_search, similar_documents
from src.ingestion.document_store import DocumentStore
from src.ingestion.embed import embed_documents


@pytest.fixture
def provider():
    return EmbeddingProvider(provider="hashing")


def _doc(doc_id, content, source="Src"):
    return Document(document_id=doc_id, source_type="news", language="en",
                    ingested_at=1_700_000_000_000, created_at=1_700_000_000_000,
                    url=f"https://ex.com/{doc_id}", title=doc_id, content=content,
                    source_id=source, metadata={"source": source})


@pytest.fixture
def wh(provider):
    conn = duckdb.connect(":memory:")
    DocumentStore(conn).upsert([
        _doc("econ1", "the economy grew as inflation cooled and jobs rose"),
        _doc("econ2", "inflation cooled while the economy grew and hiring rose"),
        _doc("sport1", "the team won the championship final in overtime"),
    ])
    embed_documents(conn, provider=provider)
    return conn


def test_search_ranks_topical_match_first(wh, provider):
    out = semantic_search(wh, "economy inflation jobs", top_k=3, provider=provider)
    assert out["count"] >= 1
    top_ids = [r["document_id"] for r in out["results"][:2]]
    assert "econ1" in top_ids and "econ2" in top_ids  # economy docs beat the sports doc
    assert out["results"][0]["source"] == "Src"       # citation metadata joined


def test_empty_index_returns_note():
    conn = duckdb.connect(":memory:")
    DocumentStore(conn)
    out = semantic_search(conn, "anything", provider=EmbeddingProvider(provider="hashing"))
    assert out["count"] == 0 and "note" in out


def test_similar_documents_excludes_self(wh):
    out = similar_documents(wh, "econ1", top_k=3)
    ids = [r["document_id"] for r in out["results"]]
    assert "econ1" not in ids
    assert ids[0] == "econ2"  # the paraphrase is the nearest neighbour


def test_similar_documents_unindexed_is_flagged(wh):
    out = similar_documents(wh, "ghost")
    assert out.get("code") == "not_indexed"


def test_near_duplicates_clusters_paraphrases(wh):
    out = near_duplicates(wh, threshold=0.5)
    assert out["count"] >= 1
    members = {d for c in out["clusters"] for d in c["document_ids"]}
    assert {"econ1", "econ2"} <= members  # the two economy paraphrases cluster
