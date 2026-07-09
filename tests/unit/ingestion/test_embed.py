"""Unit tests for the document embedding pass. Offline, in-memory DuckDB via the
deterministic hashing embedding backend (no heavy ML deps)."""

from __future__ import annotations

import duckdb
import pytest

from services.embeddings.provider import EmbeddingProvider
from services.ingest.common.document_model import Document
from src.ingestion.document_store import DocumentStore
from src.ingestion.embed import embed_documents
from src.ingestion.embedding_store import EmbeddingStore


@pytest.fixture
def provider():
    return EmbeddingProvider(provider="hashing")


def _doc(doc_id, content, source_type="news"):
    return Document(document_id=doc_id, source_type=source_type, language="en",
                    ingested_at=1_700_000_000_000, created_at=1_700_000_000_000,
                    url=f"https://ex.com/{doc_id}", title=f"title {doc_id}",
                    content=content, source_id="Src", metadata={"source": "Src"})


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    DocumentStore(c).upsert([
        _doc("d1", "Markets surge as profits gain and growth beats"),
        _doc("p1", "A study of measured warming trends over the decade", source_type="paper"),
    ])
    return c


def test_hashing_backend_is_deterministic_and_normalised(provider):
    import numpy as np

    a = provider.embed_texts(["the economy grew sharply"])
    b = provider.embed_texts(["the economy grew sharply"])
    assert a.shape == (1, provider.dim())
    assert np.allclose(a, b)                       # deterministic
    assert abs(float(np.linalg.norm(a[0])) - 1.0) < 1e-9  # L2-normalised


def test_embeds_documents_and_persists(conn, provider):
    n = embed_documents(conn, provider=provider)
    assert n == 2
    store = EmbeddingStore(conn)
    assert store.count() == 2
    rec = store.get("d1")
    assert rec["model"] == provider.name()  # "hashing:hashing" (provider:backend)
    assert rec["dim"] == provider.dim()
    assert len(rec["vector"]) == provider.dim()


def test_is_idempotent_only_fills_gaps(conn, provider):
    assert embed_documents(conn, provider=provider) == 2
    # Re-running embeds nothing new.
    assert embed_documents(conn, provider=provider) == 0
    # A newly-added document is picked up next pass.
    DocumentStore(conn).upsert([_doc("d3", "new content about elections")])
    assert embed_documents(conn, provider=provider) == 1


def test_limit_caps_the_batch(conn, provider):
    assert embed_documents(conn, provider=provider, limit=1) == 1
    assert EmbeddingStore(conn).count() == 1


def test_covers_non_news_documents(conn, provider):
    embed_documents(conn, provider=provider)
    # The paper document (non-news) is embedded, not just news.
    assert EmbeddingStore(conn).get("p1") is not None
