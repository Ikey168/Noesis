"""Revision and provider-failure regressions for #1416, #1419, #1423, #1424."""

import duckdb
import pytest

from src.ingestion.document_store import DocumentStore
from src.ingestion.embed import embed_documents
from src.ingestion.embedding_store import EmbeddingStore
from src.ingestion.enrich import enrich_documents
from src.ingestion.enrichment_store import EnrichmentStore
from src.kb import load_registry
from src.kb.membership import run_membership_pass


def document(identity, content):
    return {"document_id": identity, "source_type": "note", "language": "en",
            "ingested_at": 1000, "title": identity, "content": content}


class Provider:
    def __init__(self, model="test-a"):
        self.model = model
        self.calls = []

    def name(self):
        return self.model

    def dim(self):
        return 2

    def embed_texts(self, texts):
        self.calls.append(texts)
        return [[float(len(text)), 1.0] for text in texts]


def test_embeddings_refresh_content_model_configuration_and_legacy_rows():
    conn = duckdb.connect(":memory:")
    store = DocumentStore(conn)
    store.upsert([document("one", "A positive result.")])
    provider = Provider()
    # Old databases may have vectors without a processing receipt.
    EmbeddingStore(conn).upsert("one", [1, 0], model="test-a")
    assert embed_documents(conn, provider) == 1
    assert embed_documents(conn, provider) == 0
    before = EmbeddingStore(conn).get("one")
    store.upsert([document("one", "Correction: the previously published positive result is withdrawn.")])
    assert embed_documents(conn, provider) == 1
    assert EmbeddingStore(conn).get("one")["vector"] != before["vector"]
    assert embed_documents(conn, Provider("test-b")) == 1
    assert embed_documents(conn, Provider("test-b"), configuration={"revision": 2}) == 1
    assert embed_documents(conn, Provider("test-b"), configuration={"revision": 2}) == 0
    conn.close()


@pytest.mark.parametrize("bad", ["short", "long", "dimension", "nan", "failure"])
def test_bad_embedding_batch_preserves_prior_vectors_and_is_retryable(bad):
    conn = duckdb.connect(":memory:")
    store = DocumentStore(conn)
    store.upsert([document("one", "One."), document("two", "Two.")])
    embed_documents(conn, Provider())
    old = conn.execute("SELECT * FROM document_embeddings ORDER BY document_id").fetchall()
    store.upsert([document("one", "New one."), document("two", "New two.")])

    class Bad(Provider):
        def embed_texts(self, texts):
            if bad == "failure":
                raise RuntimeError("provider unavailable")
            return {"short": [[1, 0]], "long": [[1, 0]] * 3,
                    "dimension": [[1], [2]], "nan": [[float("nan"), 0], [1, 0]]}[bad]

    with pytest.raises((ValueError, RuntimeError)):
        embed_documents(conn, Bad())
    assert conn.execute("SELECT * FROM document_embeddings ORDER BY document_id").fetchall() == old
    assert embed_documents(conn, Provider()) == 2
    conn.close()


def test_embedding_batches_resume_after_middle_batch_failure():
    conn = duckdb.connect(":memory:")
    DocumentStore(conn).upsert([document(str(i), f"Text {i}.") for i in range(5)])

    class Flaky(Provider):
        def embed_texts(self, texts):
            if len(self.calls) == 1:
                raise RuntimeError("second batch failed")
            return super().embed_texts(texts)

    provider = Flaky()
    with pytest.raises(RuntimeError, match="second batch"):
        embed_documents(conn, provider, batch_size=2)
    assert EmbeddingStore(conn).count() == 2
    repaired = Provider()
    assert embed_documents(conn, repaired, batch_size=2, max_batch_chars=8000) == 3
    assert [len(batch) for batch in repaired.calls] == [2, 1]
    assert all(sum(len(text) for text in batch) <= 8000 for batch in repaired.calls)
    conn.close()


def test_enrichment_revision_configuration_and_failure():
    conn = duckdb.connect(":memory:")
    store = DocumentStore(conn)
    store.upsert([document("one", "Good strong growth.")])
    assert enrich_documents(conn) == 1
    assert enrich_documents(conn) == 0
    assert EnrichmentStore(conn).get("one")["sentiment_label"] == "positive"
    store.upsert([document("one", "Bad decline and losses.")])

    def fail(_):
        raise RuntimeError("analyzer failed")

    with pytest.raises(RuntimeError, match="analyzer failed"):
        enrich_documents(conn, fail, analyzer_version="new")
    assert EnrichmentStore(conn).get("one")["sentiment_label"] == "positive"
    assert enrich_documents(conn) == 1
    assert EnrichmentStore(conn).get("one")["sentiment_label"] == "negative"
    analyzer = lambda _: {"sentiment_label": "reviewed"}
    assert enrich_documents(conn, analyzer, analyzer_version="reviewed-v1") == 1
    assert enrich_documents(conn, analyzer, analyzer_version="reviewed-v1") == 0
    assert enrich_documents(conn, analyzer, analyzer_version="reviewed-v2") == 1
    conn.close()


def test_revision_moves_membership_and_tag_changes_remove_assignments(tmp_path):
    conn = duckdb.connect(":memory:")
    path = tmp_path / "domains.yml"
    path.write_text("""version: 1
domains:
  - name: economics
    backing: corpus-view
    embedding_model: test
    keywords: [inflation, rates]
  - name: gardening
    backing: corpus-view
    embedding_model: test
    tags: [gardening]
    keywords: [garden, flowers]
""")
    registry = load_registry(path)
    store = DocumentStore(conn)
    store.upsert([document("one", "Inflation rates.")])
    run_membership_pass(conn, registry)
    assert conn.execute("SELECT domain FROM document_domains").fetchall() == [("economics",)]
    store.upsert([document("one", "Garden flowers.")])
    run_membership_pass(conn, registry)
    assert conn.execute("SELECT domain FROM document_domains").fetchall() == [("gardening",)]
    assert all(x["scanned"] == 0 for x in run_membership_pass(conn, registry)["domains"].values())
    store.upsert([{**document("one", "Unrelated note."), "metadata": {"tags": ["gardening"]}}])
    run_membership_pass(conn, registry)
    assert conn.execute("SELECT score FROM document_domains").fetchone() == (1.0,)
    store.upsert([document("one", "Unrelated note.")])
    run_membership_pass(conn, registry)
    assert conn.execute("SELECT count(*) FROM document_domains").fetchone() == (0,)
    conn.close()
