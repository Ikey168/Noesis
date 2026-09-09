import duckdb
import pytest

from src.evaluation.runtime_errors import BackendError
from src.ingestion.revisions import DocumentRevisionStore
from src.kb.entity_history import EntityHistoryStore
from src.kb.optional_runtime import OptionalAnalysisStore


def setup():
    conn = duckdb.connect()
    revisions = DocumentRevisionStore(conn)
    row = revisions.observe(
        {
            "document_id": "doc",
            "content": "Original captured evidence",
            "language": "de",
            "metadata": {},
        }
    )
    return conn, revisions, [{"document_id": "doc", "revision_id": row["revision_id"]}]


def test_source_bound_input_artifact_lineage_replay_and_revocation():
    conn, revisions, refs = setup()
    calls = []

    def executor(op, payload, **bounds):
        calls.append((op, payload))
        return {"status": "completed", "result": {"entities": [], "version": "fixture"}}

    store = OptionalAnalysisStore(conn, executor=executor)
    args = {
        "namespace": "r",
        "run_id": "job",
        "operation": "gliner2",
        "payload": {"text": "caller cannot replace source", "labels": ["PERSON"]},
        "source_refs": refs,
        "principal_id": "alice",
        "scopes": {"operator"},
    }
    result = store.run(**args)
    assert calls[0][1]["text"] == "Original captured evidence"
    artifact = store.graph.inspect(result["artifact"]["artifact_id"])
    assert (
        artifact["dependencies"][0]["detail"]["revision_id"] == refs[0]["revision_id"]
    )
    assert store.run(**args)["replayed"] and len(calls) == 1
    with pytest.raises(BackendError, match="authorization"):
        store.inspect("r", "job", principal_id="alice", scopes=set())
    revisions.observe({"document_id": "doc", "content": "Changed"})
    with pytest.raises(BackendError, match="source changed"):
        store.run(**{**args, "run_id": "new"})
    conn.close()


def test_changed_source_during_model_does_not_publish_artifact():
    conn, revisions, refs = setup()

    def executor(*args, **kwargs):
        revisions.observe({"document_id": "doc", "content": "Concurrent edit"})
        return {"status": "completed", "result": {"segments": []}}

    store = OptionalAnalysisStore(conn, executor=executor)
    with pytest.raises(BackendError, match="source changed"):
        store.run(
            "r",
            "one",
            "sat",
            {},
            source_refs=refs,
            principal_id="a",
            scopes={"operator"},
        )
    assert conn.execute("SELECT count(*) FROM knowledge_artifacts").fetchone()[0] == 0
    conn.close()


def test_completed_entity_candidates_enter_review_only():
    conn, _, refs = setup()

    pytest.importorskip("rapidfuzz")
    from src.evaluation.runtime_jobs import dispatch
    from src.evaluation.runtime_outcomes import backend_outcome

    def executor(op, prepared, **kwargs):
        return backend_outcome(op, dispatch(op, prepared))

    store = OptionalAnalysisStore(conn, executor=executor)
    history = EntityHistoryStore(conn)
    for identity in ("entity:a", "entity:b"):
        history.register_entity(
            "r", identity, principal_id="alice", scopes={"operator"}
        )
    store.run(
        "r",
        "one",
        "rapidfuzz",
        {
            "source": {
                "id": "entity:a",
                "type": "Organization",
                "field_sources": {"name": {**refs[0], "pointer": "/content"}},
            },
            "candidates": [
                {
                    "id": "entity:b",
                    "type": "Organization",
                    "field_sources": {"name": {**refs[0], "pointer": "/content"}},
                }
            ],
            "threshold": 0.8,
        },
        source_refs=refs,
        principal_id="alice",
        scopes={"operator"},
    )
    task = store.queue_entity_candidate(
        "r", "one", 0, principal_id="alice", scopes={"operator"}, domain="research"
    )
    assert task["status"] == "unassigned"
    assert (
        conn.execute("SELECT count(*) FROM entity_history_redirects").fetchone()[0] == 0
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM entity_identity_decisions WHERE decision_type='review'"
        ).fetchone()[0]
        == 1
    )
    conn.close()
