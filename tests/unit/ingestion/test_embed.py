"""Unit tests for the document embedding pass. Offline, in-memory DuckDB via the
deterministic hashing embedding backend (no heavy ML deps)."""

from __future__ import annotations

import duckdb
import numpy as np
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


class StubProvider:
    def __init__(self, *, dim=2, outputs=None, fail_on_call=None):
        self._dim = dim
        self._outputs = iter(outputs) if outputs is not None else None
        self._fail_on_call = fail_on_call
        self.batch_sizes = []

    def embed_texts(self, texts):
        self.batch_sizes.append(len(texts))
        if len(self.batch_sizes) == self._fail_on_call:
            raise RuntimeError("provider failed")
        if self._outputs is not None:
            return np.asarray(next(self._outputs), dtype=float)
        return np.ones((len(texts), self._dim))

    def dim(self):
        return self._dim

    def name(self):
        return "stub"


def _add_documents(conn, count):
    DocumentStore(conn).upsert([
        _doc(f"extra-{i}", f"document {i} with enough content")
        for i in range(count)
    ])


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


def test_limit_caps_work_across_batches(conn):
    _add_documents(conn, 3)
    provider = StubProvider()

    assert embed_documents(conn, provider=provider, limit=3, batch_size=2) == 3
    assert provider.batch_sizes == [2, 1]
    assert EmbeddingStore(conn).count() == 3


def test_covers_non_news_documents(conn, provider):
    embed_documents(conn, provider=provider)
    # The paper document (non-news) is embedded, not just news.
    assert EmbeddingStore(conn).get("p1") is not None


def test_rejects_missing_vectors_without_persisting(conn):
    provider = StubProvider(outputs=[[[0.1, 0.2]]])

    with pytest.raises(ValueError, match="returned 1 vectors for 2 texts"):
        embed_documents(conn, provider=provider)

    assert EmbeddingStore(conn).count() == 0


@pytest.mark.parametrize(
    ("output", "dim", "message"),
    [
        ([0.1, 0.2], 2, "two-dimensional"),
        ([[0.1, 0.2], [0.3, 0.4]], 3, "dimension 2; expected 3"),
        ([[0.1, 0.2], [float("nan"), 0.4]], 2, "non-finite"),
        ([[0.1, 0.2], [float("inf"), 0.4]], 2, "non-finite"),
    ],
)
def test_rejects_malformed_vectors_without_persisting(conn, output, dim, message):
    provider = StubProvider(dim=dim, outputs=[output])

    with pytest.raises(ValueError, match=message):
        embed_documents(conn, provider=provider)

    assert EmbeddingStore(conn).count() == 0


def test_batches_documents_before_calling_provider(conn):
    _add_documents(conn, 3)
    provider = StubProvider()

    assert embed_documents(conn, provider=provider, batch_size=2) == 5
    assert provider.batch_sizes == [2, 2, 1]


def test_rejects_non_positive_batch_size(conn):
    with pytest.raises(ValueError, match="batch_size must be positive"):
        embed_documents(conn, provider=StubProvider(), batch_size=0)


def test_middle_batch_failure_preserves_progress_for_retry(conn):
    _add_documents(conn, 3)

    with pytest.raises(RuntimeError, match="provider failed"):
        embed_documents(conn, provider=StubProvider(fail_on_call=2), batch_size=2)

    assert EmbeddingStore(conn).count() == 2

    retry_provider = StubProvider()
    assert embed_documents(conn, provider=retry_provider, batch_size=2) == 3
    assert retry_provider.batch_sizes == [2, 1]
    assert EmbeddingStore(conn).count() == 5
