import pytest

from services.rag.qwen_rerank import BoundedQwenReranker
from src.evaluation.runtime_errors import BackendError


def test_existing_rerank_interface_preserves_original_indices_and_provenance():
    def execute(operation, payload, **limits):
        assert operation == "qwen3-reranker" and limits["timeout_s"] == 3
        items = [
            {**r, "relevance_score": 0.2 + i * 0.5}
            for i, r in enumerate(payload["candidates"])
        ]
        return {"status": "completed", "result": {"results": list(reversed(items))}}

    scorer = BoundedQwenReranker(timeout_s=3, executor=execute)
    rows = scorer.rerank(
        "q",
        [
            {"id": "a", "score": 1.0, "content": "one", "source": "s"},
            {"id": "b", "score": 0.0, "content": "two"},
        ],
    )
    assert [r.original_index for r in rows] == [1, 0]
    assert rows[1].source == "s"


def test_failure_is_not_disguised_as_fallback_quality():
    scorer = BoundedQwenReranker(
        executor=lambda *a, **k: {
            "status": "failed",
            "failure_code": "deadline_exceeded",
        }
    )
    with pytest.raises(BackendError) as exc:
        scorer.rerank("q", [{"id": "a", "content": "one"}])
    assert exc.value.code == "deadline_exceeded"
    assert scorer.last_diagnostics["status"] == "failed"
