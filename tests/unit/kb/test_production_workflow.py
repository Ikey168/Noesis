import duckdb
import pytest

from src.kb.derived_revisions import DerivedRevisionStore, DerivedRevisionError, maintenance_observations
from src.kb.workflows import WorkflowStore, reference_manifest, production_handlers
from src.kb.unified_query import MaintainedSemanticQueryAdapter


class TestSemanticProvider:
    """Injected vectors test integration contracts, not real model quality."""
    def name(self):
        return "test-semantic-v1"
    def dim(self):
        return 2
    def embed_texts(self, texts):
        return [[1.0, 0.0] if "rates" in text.lower() else [0.0, 1.0] for text in texts]


def document():
    return {"document_id": "d1", "source_type": "news", "language": "en", "ingested_at": 1,
            "content": "The bank raised rates.", "metadata": {}, "_revision_id": "source-revision-1"}


def run(conn, handlers):
    manifest = reference_manifest("research")
    manifest["stages"] = manifest["stages"][:4]
    manifest["capabilities"] = [stage["capability"] for stage in manifest["stages"]]
    return WorkflowStore(conn).execute(manifest, handlers, {"documents": [document()]}, run_key="production-test")


def test_plain_text_uses_argument_mining_and_preserves_source_revision(monkeypatch):
    from src.argument_mining.models import ClaimPrediction
    class Detector:
        prediction_mode = "test-model"
        def predict(self, doc):
            assert doc.metadata == {}
            return [ClaimPrediction(doc.content, 0, True, .95)]
    monkeypatch.setattr("src.argument_mining.evidence.get_claim_detector", lambda: Detector())
    conn = duckdb.connect()
    result = run(conn, production_handlers(conn))
    output = result["state"]["extraction"]["outputs"][0]
    assert output["output"]["value"]["statement"] == document()["content"]
    assert output["provenance"]["input_revision"] == "source-revision-1"
    assert output["output"]["value"]["source_locator"]["end"] == len(document()["content"])
    assert "argument-mining-text" in output["provenance"]["extractor_id"]
    derived = DerivedRevisionStore(conn, embedding_provider=TestSemanticProvider())
    receipt = derived.apply_generation("research", 1, maintenance_observations([document()], result["state"]["extraction"]),
                                       [{"document_id": "d1", "change_kind": "added"}])
    derived.publish_generation("research", 1)
    adapter = MaintainedSemanticQueryAdapter(conn, "research", embedding_provider=TestSemanticProvider())
    answer = adapter.query({"query": "rates", "limit": 10}, scopes={"operator"})
    assert answer["items"] and all(item["score"] == 1 for item in answer["items"])
    assert any({"document_id": "d1", "revision_id": "source-revision-1"} in item["citations"] for item in answer["items"])


def test_unavailable_model_is_an_explicit_failed_stage(monkeypatch):
    def unavailable():
        raise RuntimeError("model unavailable")
    monkeypatch.setattr("src.argument_mining.evidence.get_claim_detector", unavailable)
    conn = duckdb.connect()
    with pytest.raises(Exception, match="extractor"):
        run(conn, production_handlers(conn))
    assert conn.execute("SELECT count(*) FROM knowledge_extractor_outputs WHERE status='failed'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM knowledge_workflow_watermarks").fetchone()[0] == 0


def test_vectors_are_provider_backed_and_mixed_spaces_fail_atomically():
    conn = duckdb.connect()
    store = DerivedRevisionStore(conn, embedding_provider=TestSemanticProvider())
    obs = {"object_type": "claim", "content": {"statement": "Rates increased"}, "document_id": "d1", "source_revision_id": "r1", "producer": {"name": "test-extractor", "version": "1"}}
    store.apply_generation("research", 1, [obs], [{"document_id": "d1"}])
    store.publish_generation("research", 1)
    vectors = store.projection("research", "vector")
    assert vectors[0]["content"]["vector"] == [1.0, 0.0]
    assert vectors[0]["content"]["synthetic"] is False
    store.embedding_configuration = {"tokenizer_revision": "changed"}
    with pytest.raises(DerivedRevisionError, match="embedding space"):
        store.apply_generation("research", 2, [{**obs, "source_revision_id": "r2"}], [{"document_id": "d1"}])
    with pytest.raises(DerivedRevisionError, match="embedding space"):
        store.semantic_search("research", "rates", scopes={"operator"})
    assert conn.execute("SELECT count(*) FROM derived_object_generations").fetchone()[0] == 1


def test_hash_backend_requires_fixture_mode():
    provider = TestSemanticProvider()
    provider.name = lambda: "hashing:sha256"
    store = DerivedRevisionStore(duckdb.connect(), embedding_provider=provider)
    with pytest.raises(DerivedRevisionError, match="semantic model"):
        store.apply_generation("research", 1, [], [])
