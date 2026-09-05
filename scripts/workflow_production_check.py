#!/usr/bin/env python3
"""Opt-in real-model integration check; not an independently judged quality benchmark."""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check():
    import duckdb
    from services.embeddings.provider import get_embedding_provider
    from src.kb.derived_revisions import DerivedRevisionStore, maintenance_observations
    from src.kb.workflows import WorkflowStore, WorkflowError, reference_manifest, production_handlers
    from src.argument_mining.model_registry import resolved_pins

    documents = json.loads((ROOT / "tests/fixtures/workflow_real_text/corpus.json").read_text())
    for document in documents:
        assert hashlib.sha256(document["content"].encode()).hexdigest() == document["metadata"]["pinned_text_sha256"]
        assert "knowledge" not in document["metadata"]
        document["_revision_id"] = "pinned:" + document["metadata"]["pinned_text_sha256"]
    start = time.monotonic()
    provider = get_embedding_provider(provider="local", model_name="all-MiniLM-L6-v2")
    with tempfile.TemporaryDirectory(prefix="noesis-production-check-") as temporary:
        conn = duckdb.connect(str(Path(temporary) / "warehouse.duckdb"))
        workflows = WorkflowStore(conn)
        manifest = reference_manifest("production-check")
        manifest["workflow_id"] = "production-model-conformance"
        manifest["domains"] = ["economic", "scientific"]
        handlers = production_handlers(conn)
        initial = {"documents": documents}
        try:
            workflows.execute(manifest, handlers, initial, run_key="baseline", fail_after=2)
        except WorkflowError:
            pass
        result = workflows.execute(manifest, handlers, initial, run_key="baseline")
        replay = workflows.execute(manifest, handlers, initial, run_key="baseline")
        assert result["run_id"] == replay["run_id"] and result["status"] == "completed"
        outputs = [out for out in result["state"]["extraction"]["outputs"] if out["status"] == "produced"]
        assert outputs, "real model produced no claims for this corpus"
        assert result["state"]["report"]["verified"]
        store = DerivedRevisionStore(conn, embedding_provider=provider)
        changes = [{"document_id": doc["document_id"], "change_kind": "added"} for doc in documents]
        baseline = store.apply_generation("production-check", 1, maintenance_observations(documents, result["state"]["extraction"]), changes)
        store.publish_generation("production-check", 1)
        checks = []
        for query, expected in [("employment and inflation in the economy", "fomc-20220615"),
                                ("infrared telescope photograph of distant galaxies", "webb-20220712")]:
            hits = store.semantic_search("production-check", query, scopes={"operator"}, limit=1)
            matched = bool(hits) and expected in {citation["document_id"] for citation in hits[0]["citations"]}
            checks.append({"query": query, "expected_document": expected, "matched": matched,
                           "top_score": hits[0]["score"] if hits else None})
        assert all(item["matched"] for item in checks), checks
        from src.ingestion.document_store import DocumentStore
        from src.ingestion.chunk_embeddings import embed_document_chunks, search_document_chunks
        # Synthetic stress input is kept separate from the pinned source corpus.
        tail = "Routine accounting records and quarterly inventory totals. " * 160
        tail += "The zephyr observatory detected an infrared galaxy behind a gravitational lens."
        DocumentStore(conn).upsert([{"document_id": "synthetic-tail", "source_type": "note",
            "language": "en", "ingested_at": 1, "title": "Synthetic retrieval stress test", "content": tail}])
        embed_document_chunks(conn, provider, document_ids=["synthetic-tail"])
        tail_hit = search_document_chunks(conn, "infrared galaxy gravitational lens", provider,
            document_ids=["synthetic-tail"])["results"][0]
        assert tail_hit["start_offset"] > 4000 and "gravitational lens" in tail_hit["text"]
        assert tail[tail_hit["start_offset"]:tail_hit["end_offset"]] == tail_hit["text"]
        # Lifecycle fault exercises operate on test copies. They do not assert
        # that either publisher corrected or retracted these original articles.
        corrected = json.loads(json.dumps(documents))
        corrected[0]["metadata"]["test_copy_correction"] = "metadata correction exercise"
        corrected[0]["_revision_id"] = "test-correction:2"
        update = workflows.execute(manifest, handlers, {"documents": corrected}, run_key="correction")
        store.apply_generation("production-check", 2, maintenance_observations(corrected, update["state"]["extraction"]),
                               [{"document_id": doc["document_id"], "change_kind": "corrected"} for doc in corrected])
        store.publish_generation("production-check", 2)
        retraction = store.apply_generation("production-check", 3, [], [{"document_id": documents[1]["document_id"], "change_kind": "retracted"}])
        store.publish_generation("production-check", 3)
        assert retraction["counts"].get("retracted", 0) > 0
        assert not any(citation["document_id"] == documents[1]["document_id"] for item in store.semantic_search(
            "production-check", "infrared telescope", scopes={"operator"}) for citation in item["citations"])
        assert update["state"]["report"]["verified"]
        extracted_modes = sorted({out["output"]["value"]["prediction_mode"] for out in outputs})
        return {"status": "passed", "validation_kind": "real-model-integration-smoke-test",
                "independent_human_judgments": False, "domains": manifest["domains"],
                "source_urls": [doc["url"] for doc in documents],
                "model_pins": resolved_pins(), "claim_prediction_modes": extracted_modes,
                "embedding_model": provider.name(), "embedding_dimensions": provider.dim(),
                "library_versions": {name: importlib.metadata.version(name) for name in ("torch", "sentence-transformers", "transformers")},
                "claims_produced": len(outputs), "semantic_checks": checks,
                "synthetic_full_document_check": {"matched_tail": True,
                    "start_offset": tail_hit["start_offset"], "score": tail_hit["score"],
                    "tokenizer": provider.tokenizer_identity()},
                "resumed_execution": True, "replay_same_run": True,
                "correction_generation": 2, "test_copy_retraction_counts": retraction["counts"],
                "subscription_events": result["state"]["report"]["subscription_events"],
                "export_verified": True, "elapsed_seconds": round(time.monotonic() - start, 3),
                "limitations": ["Small handpicked corpus; no independent retrieval or claim-quality judgments",
                                "Lifecycle mutations are explicitly simulated on test copies",
                                "Model-unavailable and partial-coverage behavior are covered separately by unit tests"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = check()
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
