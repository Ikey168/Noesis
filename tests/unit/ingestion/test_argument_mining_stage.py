"""Incremental post-ingest argument-mining contract (issue #998)."""

import duckdb
import pytest

from src.ingestion.argument_mining import mine_unprocessed_documents
from src.ingestion.document_store import DocumentStore


@pytest.mark.parametrize("failure", ["extraction", "classification", "persistence"])
def test_failed_replacement_preserves_prior_claims_and_evidence(monkeypatch, failure):
    from src.database.local_warehouse_seed import ensure_schema

    conn = duckdb.connect(":memory:")
    store = DocumentStore(conn)
    store.upsert([_document("The revised policy evidence.")])
    ensure_schema(conn)
    conn.execute("INSERT INTO argument_claims(claim_id,claim_text,document_id,source_type) VALUES ('old','Prior evidence','incremental-1','news')")
    conn.execute("INSERT INTO claim_evidence(evidence_id,claim_id,evidence_document_id,evidence_source_type,relation) VALUES ('old-e','old','incremental-1','news','supports')")

    def extract(*args):
        if failure == "extraction":
            raise RuntimeError("injected extraction failure")
        conn.execute("INSERT INTO argument_claims(claim_id,claim_text,document_id,source_type) VALUES ('new','New evidence','incremental-1','news')")
        return [], []

    def classify(*args):
        if failure == "persistence":
            conn.execute("INSERT INTO nonexistent_review_table VALUES (1)")
        raise RuntimeError("injected classifier failure")

    monkeypatch.setattr("src.argument_mining.evidence.run_pipeline", extract)
    monkeypatch.setattr("src.argument_mining.frames.classify_and_store", classify)
    result = mine_unprocessed_documents(conn, limit=1)
    assert result["status"] == "partial" and result["failed"] == 1
    assert conn.execute("SELECT claim_id FROM argument_claims").fetchall() == [("old",)]
    assert conn.execute("SELECT evidence_id FROM claim_evidence").fetchall() == [("old-e",)]
    assert conn.execute("SELECT status FROM argument_mining_scans").fetchone()[0] == "failed"
    monkeypatch.setattr("src.argument_mining.evidence.run_pipeline", lambda *_: ([], []))
    monkeypatch.setattr("src.argument_mining.frames.classify_and_store", lambda *_: None)
    monkeypatch.setattr("src.argument_mining.models.get_claim_detector", lambda: type("Detector", (), {"prediction_mode": "test"})())
    assert mine_unprocessed_documents(conn, limit=1)["processed"] == 1
    assert conn.execute("SELECT count(*) FROM argument_claims").fetchone()[0] == 0
    conn.close()


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
    class Detector:
        prediction_mode = "pretrained:test-claim-model"

    monkeypatch.setattr("src.argument_mining.evidence.run_pipeline", lambda *_args: ([], []))
    monkeypatch.setattr("src.argument_mining.frames.classify_and_store", lambda *_args: None)
    monkeypatch.setattr("src.argument_mining.models.get_claim_detector", lambda: Detector())
    conn = duckdb.connect()
    store = DocumentStore(conn)
    store.upsert([_document("The central bank raised rates by 0.75 percent in June.")])

    first = mine_unprocessed_documents(conn, limit=10)
    assert first["processed"] == 1
    assert first["failed"] == 0
    assert first["documents_mined"] == 1
    assert first["freshness_ratio"] == 1.0
    assert first["graph_documents_pending"] == 0
    assert conn.execute("SELECT count(*) FROM kg_nodes").fetchone()[0] > 0

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


def test_graph_failure_retries_after_restart_without_repeating_inference(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("src.argument_mining.evidence.run_pipeline", lambda doc, *_: (calls.append(doc.document_id) or [], []))
    monkeypatch.setattr("src.argument_mining.frames.classify_and_store", lambda *_: None)
    monkeypatch.setattr("src.argument_mining.models.get_claim_detector", lambda: type("Detector", (), {"prediction_mode": "test"})())
    path = str(tmp_path / "warehouse.duckdb")
    conn = duckdb.connect(path)
    DocumentStore(conn).upsert([_document("John Smith published a policy decision.")])

    def unavailable(_):
        raise RuntimeError("graph unavailable")

    result = mine_unprocessed_documents(conn, graph_publisher=unavailable)
    assert result["processed"] == 1
    assert result["graph_documents_pending"] == 1
    assert result["graph_projection"]["failed"] == 1
    assert conn.execute("SELECT attempts,last_error FROM argument_graph_projections").fetchone() == (1, "graph unavailable")
    conn.close()
    conn = duckdb.connect(path)
    published = []
    result = mine_unprocessed_documents(conn, graph_publisher=published.append)
    assert result["processed"] == 0
    assert result["graph_documents_pending"] == 0
    assert result["graph_projection"]["completed"] == 1
    assert calls == ["incremental-1"]
    assert published[0]["content"] == "John Smith published a policy decision."
    assert mine_unprocessed_documents(conn, graph_publisher=published.append)["graph_projection"]["completed"] == 0
    assert len(published) == 1
    conn.close()


def test_failed_inference_does_not_enqueue_graph_work(monkeypatch):
    def failure(*_):
        raise RuntimeError("inference unavailable")
    monkeypatch.setattr("src.argument_mining.evidence.run_pipeline", failure)
    conn = duckdb.connect()
    DocumentStore(conn).upsert([_document("New content.")])
    mine_unprocessed_documents(conn, graph_publisher=lambda _: pytest.fail("must not project failed inference"))
    assert conn.execute("SELECT count(*) FROM argument_graph_projections").fetchone()[0] == 0
    conn.close()


def test_model_and_configuration_changes_invalidate_unchanged_text(monkeypatch):
    class Detector:
        prediction_mode = "pretrained:model-one"
    detector = Detector()
    monkeypatch.setattr("src.argument_mining.models.get_claim_detector", lambda: detector)
    monkeypatch.setattr("src.argument_mining.evidence.run_pipeline", lambda *_: ([], []))
    monkeypatch.setattr("src.argument_mining.frames.classify_and_store", lambda *_: None)
    conn = duckdb.connect()
    DocumentStore(conn).upsert([_document("Same source text throughout.")])
    assert mine_unprocessed_documents(conn)["processed"] == 1
    assert mine_unprocessed_documents(conn)["processed"] == 0
    detector.prediction_mode = "pretrained:model-two"
    assert mine_unprocessed_documents(conn)["processed"] == 1
    assert mine_unprocessed_documents(conn)["processed"] == 0
    assert mine_unprocessed_documents(conn, configuration={"rules_version": 2})["processed"] == 1
    assert mine_unprocessed_documents(conn, configuration={"rules_version": 2})["processed"] == 0
    conn.execute("UPDATE documents SET title='A revised title'")
    assert mine_unprocessed_documents(conn, configuration={"rules_version": 2})["processed"] == 1
    conn.close()
