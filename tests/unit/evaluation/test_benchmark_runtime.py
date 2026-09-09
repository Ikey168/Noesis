import copy

import pytest

from src.evaluation.benchmark_runtime import (
    evaluate_manifest,
    retrieval_job,
    score_output,
)


def manifest():
    return {
        "contract": "noesis-native-benchmark-v1",
        "configuration": {"development_groups": ["dev"]},
        "cases": [
            {
                "id": "case",
                "group_id": "held-out",
                "split": "test",
                "operation": "qwen3-reranker",
                "metric": "ranking",
                "payload": {"query": "Query", "candidates": []},
                "expected": {"relevant": 1, "unrelated": 0},
                "source": "fixture-source",
                "domain": "research",
                "language": "de",
                "label_origin": "fixture",
            }
        ],
    }


def test_ranking_and_exact_span_metrics_and_partial_outcome_honesty():
    cases = manifest()

    def executor(*args, **kwargs):
        return {
            "status": "completed",
            "result": [{"id": "unrelated"}, {"id": "relevant"}],
            "elapsed_seconds": 0.1,
        }

    result = evaluate_manifest(cases, executor=executor)
    assert (
        result["cases"][0]["metrics"]["mrr_at_k"] == 0.5
        and not result["automatic_adoption"]
    )
    assert result["executor_kind"] == "injected-fixture-executor"
    cases["cases"].append({**copy.deepcopy(cases["cases"][0]), "id": "failed"})
    state = [0]

    def incomplete(*args, **kwargs):
        state[0] += 1
        return (
            executor()
            if state[0] == 1
            else {"status": "unavailable", "failure_code": "model_unavailable"}
        )

    result = evaluate_manifest(cases, executor=incomplete)
    assert (
        result["status"] == "partial" and result["summary"]["ranking"]["means"] is None
    )
    spans = score_output(
        "spans",
        [{"start": 0, "end": 3, "label": "ORG"}],
        [{"start": 0, "end": 4, "label": "ORG"}],
    )
    assert spans["f1"] == 0
    assert score_output("text", "Mueller", "Müller")["character_edits"] == 2


def test_development_group_leakage_is_rejected_before_execution():
    cases = manifest()
    cases["cases"][0]["group_id"] = "dev"
    with pytest.raises(ValueError, match="held-out"):
        evaluate_manifest(
            cases, executor=lambda *a, **k: pytest.fail("must not execute")
        )


def test_embedding_retrieval_executes_index_and_preserves_source_ids():
    import numpy as np

    class Model:
        space_id = "a" * 64

        def embed_texts(self, texts):
            return np.asarray(
                [[1.0, 0.0] if value == "target" else [0.0, 1.0] for value in texts]
            )

        embed_queries = embed_texts

    result = retrieval_job(
        {
            "backend": "e5",
            "documents": [
                {"id": "a", "revision": "one", "text": "other"},
                {"id": "b", "revision": "two", "text": "target"},
            ],
            "query": "target",
            "limit": 30,
        },
        model=Model(),
    )
    assert (
        result["results"][0]["id"] == "b" and result["results"][0]["revision"] == "two"
    )
    assert (
        result["serialized_index_bytes"] > 0 and not result["production_index_modified"]
    )


@pytest.mark.parametrize("size", [129, 200])
def test_bge_benchmark_batches_the_whole_supported_corpus(size):
    from types import SimpleNamespace

    from src.evaluation.model_backends import BGEBackend

    calls = []

    class NativeModel:
        tokenizer = SimpleNamespace(encode=lambda text, **kw: [1, 2, 3])

        def encode(self, texts, **kwargs):
            calls.append(len(texts))
            return {
                "dense_vecs": [[1.0, 0.0] for _ in texts],
                "lexical_weights": [{"1": 1.0} for _ in texts],
                "colbert_vecs": [[[1.0, 0.0]] for _ in texts],
            }

    backend = BGEBackend(model=NativeModel())
    result = retrieval_job(
        {
            "backend": "bge-m3",
            "documents": [
                {"id": str(i), "revision": "r1", "text": "Captured text"}
                for i in range(size)
            ],
            "query": "Query",
            "limit": size,
        },
        model=backend,
    )
    assert len(result["results"]) == size and sum(calls[:-1]) == size
    assert max(calls) <= 128
    assert result["timing_contract"] == "noesis-retrieval-timing-v2"
    assert result["query_seconds"] == pytest.approx(
        result["query_encoding_seconds"] + result["search_seconds"]
    )
    assert result["encoding_seconds"] == pytest.approx(
        result["model_load_seconds"]
        + result["corpus_encoding_seconds"]
        + result["query_encoding_seconds"]
    )


def test_partial_nested_output_not_scored_as_complete():
    cases = manifest()
    cases["cases"][0].update(
        operation="lightonocr",
        metric="text",
        expected="unfinished",
        output_pointer="/pages/0/text",
    )
    result = evaluate_manifest(
        cases,
        executor=lambda *a, **k: {
            "status": "completed",
            "result": {
                "complete": False,
                "pages": [
                    {"status": "partial", "truncated": True, "text": "unfinished"}
                ],
            },
        },
    )
    assert result["status"] == "partial" and result["cases"][0]["metrics"] is None
    assert result["summary"]["text"]["means"] is None
