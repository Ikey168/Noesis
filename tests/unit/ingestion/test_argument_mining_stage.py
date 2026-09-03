"""Incremental post-ingest argument-mining contract (issue #998)."""

import duckdb

from src.ingestion.argument_mining import mine_unprocessed_documents
from src.ingestion.document_store import DocumentStore


def _document(content: str):
    return {
        "document_id": "incremental-1",
        "source_type": "news",
        "language": "en",
        "ingested_at": 1,
        "source_id": "wire",
        "title": "Rates decision",
        "content": content,
        "metadata": {},
    }


def test_stage_mines_once_and_reprocesses_a_revision(monkeypatch):
    monkeypatch.setenv("NOESIS_CLAIMS_BACKEND", "heuristic")
    monkeypatch.setenv("NOESIS_STANCE_BACKEND", "heuristic")
    monkeypatch.setenv("NOESIS_FRAMES_BACKEND", "heuristic")
    conn = duckdb.connect()
    store = DocumentStore(conn)
    store.upsert([_document("The central bank raised rates by 0.75 percent in June.")])

    first = mine_unprocessed_documents(conn, limit=10)
    assert first["processed"] == 1
    assert first["failed"] == 0
    assert first["documents_mined"] == 1
    assert first["freshness_ratio"] == 1.0

    assert mine_unprocessed_documents(conn, limit=10)["processed"] == 0

    from src.ingestion.canonical import content_hash

    revised_text = "The central bank cut rates by 0.25 percent in July."
    conn.execute(
        "UPDATE documents SET content = ?, content_hash = ? WHERE document_id = ?",
        [revised_text, content_hash(revised_text), "incremental-1"],
    )
    revised = mine_unprocessed_documents(conn, limit=10)
    assert revised["processed"] == 1
    assert revised["documents_pending"] == 0


def test_stage_has_an_explicit_lean_install_opt_out(monkeypatch):
    monkeypatch.setenv("NOESIS_ARGUMENT_MINING_ENABLED", "false")
    conn = duckdb.connect()
    DocumentStore(conn).upsert([_document("The bank published a policy decision.")])
    result = mine_unprocessed_documents(conn)
    assert result["status"] == "disabled"
    assert result["processed"] == 0
    assert result["documents_pending"] == 1
