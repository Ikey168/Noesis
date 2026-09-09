from types import SimpleNamespace

import numpy as np
import pytest

from services.rag.retriever import HybridRetriever, PartialRetrievalError
from tests.unit.services.rag.test_retriever_comprehensive import vec_result


def test_more_than_twenty_and_numpy_query_with_explicit_partial_failure():
    seen = []

    def vector(query, k, filters):
        seen.append(k)
        return [vec_result(str(i), 1 - i / 100) for i in range(k)]

    retriever = HybridRetriever(vector_service=SimpleNamespace(search=vector))
    result = retriever.search_detailed("query", np.array([0.1, 0.2]), k=35)
    assert (
        len(result["results"]) == 35 and seen == [70] and result["status"] == "complete"
    )

    def failed(*args):
        raise RuntimeError("private provider detail")

    retriever.lexical_service = SimpleNamespace(search=failed)
    response = retriever.search_detailed("query", [0.1], k=35)
    assert response["status"] == "partial" and len(response["results"]) == 35
    assert "private provider detail" not in str(response)
    with pytest.raises(PartialRetrievalError) as error:
        retriever.search("query", [0.1])
    assert error.value.response["status"] == "partial"


def test_zero_matches_differs_from_unavailable_and_missing_embedding():
    assert HybridRetriever().search_detailed("query")["status"] == "unavailable"
    retriever = HybridRetriever(vector_service=SimpleNamespace(search=lambda *a: []))
    assert retriever.search_detailed("query", [0.1])["status"] == "complete"
    assert retriever.search_detailed("query")["status"] == "partial"


def test_reranker_failure_cannot_masquerade_as_reranked_result():
    from services.rag.rerank import CrossEncoderReranker

    model = object.__new__(CrossEncoderReranker)
    model.is_enabled = True
    model.model = None
    retriever = HybridRetriever(
        vector_service=SimpleNamespace(search=lambda *a: [vec_result("d", 0.9)]),
        reranker=model,
    )
    response = retriever.search_detailed("query", [0.1])
    assert (
        response["status"] == "partial"
        and response["sources"][-1]["source"] == "reranker"
    )


@pytest.mark.parametrize(
    "scores", [[], [float("nan")], [float("inf")], [[0.5]], [0.5, 0.4]]
)
def test_malformed_native_reranker_scores_preserve_fusion_with_partial_diagnostics(
    scores,
):
    from services.rag.rerank import CrossEncoderReranker

    model = object.__new__(CrossEncoderReranker)
    model.is_enabled = True
    model.model = SimpleNamespace(predict=lambda pairs: scores)
    retriever = HybridRetriever(
        vector_service=SimpleNamespace(search=lambda *a: [vec_result("d", 0.9)]),
        reranker=model,
    )
    response = retriever.search_detailed("query", [0.1])
    assert response["status"] == "partial"
    assert len(response["results"]) == 1
    assert response["results"][0].id == "d:c1"
    assert response["sources"][-1] == {
        "source": "reranker",
        "status": "failed",
        "error_type": "ValueError",
    }
