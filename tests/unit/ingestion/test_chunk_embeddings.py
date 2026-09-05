import math

import duckdb
import pytest

from src.ingestion.document_store import DocumentStore
from src.ingestion.chunk_embeddings import embed_document_chunks, search_document_chunks, publish_chunk_receipts
from src.ingestion.token_chunks import token_chunks


class Provider:
    def __init__(self):
        self.calls = []
        self.fail_at = None
        self.revision = 1
    def name(self):
        return "test-semantic"
    def dim(self):
        return 2
    def token_limit(self):
        return 32
    def count_tokens(self, text):
        return math.ceil(len(text.encode()) / 4)
    def tokenizer_identity(self):
        return {"name": "test-byte-budget", "revision": self.revision, "max_tokens": 32}
    def embed_texts(self, texts):
        self.calls.append(list(texts))
        if len(self.calls) == self.fail_at:
            raise RuntimeError("provider failure")
        assert all(self.count_tokens(text) <= self.token_limit() for text in texts)
        return [[1.0, 0.0] if "zephyr" in text else [0.0, 1.0] for text in texts]


def doc(content, identifier="d1"):
    return {"document_id": identifier, "source_type": "note", "language": "en", "ingested_at": 1,
            "content": content, "title": "Long source"}


@pytest.mark.parametrize("text", ["界🙂 café " * 100, "x" * 1000, "a.  repeated a.\n" * 100])
def test_token_budget_offsets_overlap_and_full_coverage(text):
    provider = Provider()
    chunks = list(token_chunks(text, provider, overlap_tokens=4))
    covered = set()
    for chunk in chunks:
        assert chunk["text"] == text[chunk["start_offset"]:chunk["end_offset"]]
        assert provider.count_tokens(chunk["text"]) <= 32
        covered.update(range(chunk["start_offset"], chunk["end_offset"]))
    assert covered == set(range(len(text)))
    assert all(left["start_offset"] < right["start_offset"] for left, right in zip(chunks, chunks[1:]))


def test_tail_retrieval_after_4000_characters_and_scope(tmp_path):
    conn = duckdb.connect(str(tmp_path / "chunks.duckdb"))
    text = "ordinary background content. " * 200 + "The zephyr measurement appears only at the end."
    DocumentStore(conn).upsert([doc(text), doc("Unrelated background", "other")])
    provider = Provider()
    result = embed_document_chunks(conn, provider, overlap_tokens=4, batch_size=2)
    assert result["processed"] == 2
    calls = len(provider.calls)
    assert embed_document_chunks(conn, provider, overlap_tokens=4)["processed"] == 0
    assert len(provider.calls) == calls
    found = search_document_chunks(conn, "zephyr", provider, top_k=25)
    hit = found["results"][0]
    assert hit["document_id"] == "d1" and hit["start_offset"] > 4000
    assert text[hit["start_offset"]:hit["end_offset"]] == hit["text"]
    assert hit["source_revision_id"] and found["coverage"]["complete"]
    scoped = search_document_chunks(conn, "zephyr", provider, document_ids=["other"])
    assert {hit["document_id"] for hit in scoped["results"]} == {"other"}
    assert all(len(batch) <= 2 for batch in provider.calls[:calls])
    conn.close()


def test_failed_batch_preserves_old_generation_and_resumes_staged_vectors():
    conn = duckdb.connect()
    documents = DocumentStore(conn)
    documents.upsert([doc("Original zephyr statement.")])
    provider = Provider()
    embed_document_chunks(conn, provider, overlap_tokens=4, batch_size=2)
    before = conn.execute("SELECT input_hash,configuration_hash FROM document_processing_versions WHERE stage='chunk_embedding'").fetchone()
    documents.upsert([doc("New evidence. " * 70 + "zephyr at the tail")])
    provider.fail_at = len(provider.calls) + 2
    with pytest.raises(RuntimeError, match="provider failure"):
        embed_document_chunks(conn, provider, overlap_tokens=4, batch_size=2)
    assert conn.execute("SELECT input_hash,configuration_hash FROM document_processing_versions WHERE stage='chunk_embedding'").fetchone() == before
    assert search_document_chunks(conn, "zephyr", Provider())["coverage"]["pending_documents"] == 1
    staged = set(conn.execute("SELECT chunk_id FROM document_chunk_embeddings WHERE input_hash<>?", [before[0]]).fetchall())
    assert staged
    provider.fail_at = None
    resumed = embed_document_chunks(conn, provider, overlap_tokens=4, batch_size=2)
    assert resumed["processed"] == 1
    assert conn.execute("SELECT count(*) FROM document_chunk_embeddings WHERE input_hash=?", [before[0]]).fetchone()[0] == 0
    assert search_document_chunks(conn, "zephyr", provider)["coverage"]["complete"]


def test_publication_can_join_maintenance_transaction_and_tokenizer_changes_refresh():
    conn = duckdb.connect()
    DocumentStore(conn).upsert([doc("A zephyr source.")])
    provider = Provider()
    staged = embed_document_chunks(conn, provider, overlap_tokens=4, publish=False)
    assert search_document_chunks(conn, "zephyr", provider)["results"] == []
    conn.execute("BEGIN")
    publish_chunk_receipts(conn, staged["receipts"])
    conn.execute("ROLLBACK")
    assert search_document_chunks(conn, "zephyr", provider)["results"] == []
    conn.execute("BEGIN")
    publish_chunk_receipts(conn, staged["receipts"])
    conn.execute("COMMIT")
    assert search_document_chunks(conn, "zephyr", provider)["count"] == 1
    provider.revision = 2
    assert search_document_chunks(conn, "zephyr", provider)["coverage"]["pending_documents"] == 1
    assert embed_document_chunks(conn, provider, overlap_tokens=4)["processed"] == 1


def test_chunk_count_overflow_and_source_race_do_not_publish():
    conn = duckdb.connect()
    store = DocumentStore(conn)
    store.upsert([doc("Many words. " * 100)])
    provider = Provider()
    with pytest.raises(ValueError, match="chunk count"):
        embed_document_chunks(conn, provider, overlap_tokens=4, max_chunks=1)
    staged = embed_document_chunks(conn, provider, overlap_tokens=4, publish=False)
    store.upsert([doc("A source correction")])
    with pytest.raises(ValueError, match="source changed"):
        publish_chunk_receipts(conn, staged["receipts"])
    assert conn.execute("SELECT count(*) FROM document_processing_versions WHERE stage='chunk_embedding'").fetchone()[0] == 0
