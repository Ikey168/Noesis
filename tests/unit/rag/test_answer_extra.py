"""Focused unit tests for legacy/services/rag/answer.py (RAGAnswerService).

These tests exercise the orchestration helpers and the full answer_question
pipeline with mocked embeddings provider and MLflow tracking so that no real
network/MLflow backend is required.
"""

import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock

import numpy as np
import pytest

# services/ is a top-level source root; make `from services...` importable.
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
sys.path.insert(0, ROOT)

from legacy.services.rag.answer import RAGAnswerService  # noqa: E402


def make_service(**kwargs):
    """Build a service with a mocked embeddings provider (no real model load)."""
    fake_provider = MagicMock()
    # encode returns a deterministic numpy vector regardless of input text.
    fake_provider.model.encode.return_value = np.array([0.1, 0.2, 0.3], dtype=float)
    kwargs.setdefault("embeddings_provider", fake_provider)
    return RAGAnswerService(**kwargs)


@pytest.fixture
def service():
    return make_service()


# ---------------------------------------------------------------------------
# Construction / defaults
# ---------------------------------------------------------------------------

def test_init_defaults_and_metrics_structure():
    svc = make_service(default_k=7, rerank_enabled=False, fusion_enabled=False,
                       answer_provider="anthropic")
    assert svc.default_k == 7
    assert svc.rerank_enabled is False
    assert svc.fusion_enabled is False
    assert svc.answer_provider == "anthropic"
    # metrics dictionary initialised to zeros
    assert svc.metrics["retrieval_ms"] == 0.0
    assert svc.metrics["num_citations"] == 0
    assert set(svc.artifacts.keys()) == {"citations", "retrieval_trace", "query_processing"}


def test_get_metrics_returns_copy(service):
    m = service.get_metrics()
    m["k_used"] = 999
    # mutating the returned dict must not affect internal state
    assert service.metrics["k_used"] == 0


def test_reset_metrics_clears_state(service):
    service.metrics["k_used"] = 5
    service.artifacts["citations"] = [1, 2, 3]
    service._reset_metrics()
    assert service.metrics["k_used"] == 0
    assert service.artifacts["citations"] == []


# ---------------------------------------------------------------------------
# _classify_question_type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question,expected", [
    ("What is AI?", "factual"),
    ("Which model is best?", "factual"),
    ("How does it work?", "explanatory"),
    ("Why is the sky blue?", "explanatory"),
    ("When did it happen?", "temporal_spatial"),
    ("Where is it located?", "temporal_spatial"),
    ("Who invented this?", "entity"),
    ("Is this correct?", "general_question"),
    ("Tell me about energy", "statement_query"),
])
def test_classify_question_type(service, question, expected):
    assert service._classify_question_type(question) == expected


# ---------------------------------------------------------------------------
# _perform_vector_search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_perform_vector_search_returns_k_sorted(service):
    emb = np.array([0.1, 0.2, 0.3])
    docs = await service._perform_vector_search(emb, k=5, filters=None)
    assert len(docs) == 5
    # scores are monotonically decreasing in the sample generator
    scores = [d["score"] for d in docs]
    assert scores == sorted(scores, reverse=True)
    # required keys present
    for d in docs:
        assert {"id", "title", "content", "source", "score", "category"} <= set(d)


@pytest.mark.asyncio
async def test_perform_vector_search_filter_applied(service):
    emb = np.array([0.1, 0.2, 0.3])
    docs = await service._perform_vector_search(emb, k=4, filters={"category": "science"})
    assert len(docs) == 4
    # Every returned doc must match the requested category (filtered + padded).
    assert all(d["category"] == "science" for d in docs)


@pytest.mark.asyncio
async def test_perform_vector_search_padding_when_filter_too_strict(service):
    emb = np.array([0.1, 0.2, 0.3])
    # A category that matches nothing forces the padding branch.
    docs = await service._perform_vector_search(emb, k=3, filters={"category": "nonexistent"})
    assert len(docs) == 3
    assert all(d["category"] == "nonexistent" for d in docs)


# ---------------------------------------------------------------------------
# _expand_query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expand_query_limits_to_two(service):
    expanded = await service._expand_query("Quantum Computing")
    assert len(expanded) == 2
    assert expanded[0] == "Quantum Computing"  # original kept first


# ---------------------------------------------------------------------------
# _retrieve_documents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retrieve_documents_with_fusion(service):
    docs = await service._retrieve_documents("What is ML?", k=3, filters=None, fusion=True)
    assert len(docs) == 3
    qp = service.artifacts["query_processing"]
    assert qp["original_query"] == "What is ML?"
    assert qp["fusion_enabled"] is True
    assert "expanded_queries" in qp
    # fusion records a non-negative timing
    assert service.metrics["fusion_time_ms"] >= 0.0
    # encode was invoked (once per expanded query)
    assert service.embeddings_provider.model.encode.called
    # retrieval trace populated with ranks
    assert [t["rank"] for t in service.artifacts["retrieval_trace"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_retrieve_documents_without_fusion(service):
    docs = await service._retrieve_documents("Explain energy", k=2, filters=None, fusion=False)
    assert len(docs) == 2
    qp = service.artifacts["query_processing"]
    assert qp["fusion_enabled"] is False
    assert "expanded_queries" not in qp
    # encode called exactly once (single non-expanded query)
    assert service.embeddings_provider.model.encode.call_count == 1


@pytest.mark.asyncio
async def test_retrieve_documents_long_title_truncation(service):
    long_title = "T" * 300
    # Patch vector search to return a doc with an overly long title.
    async def fake_search(emb, k, filters):
        return [{"id": "x1", "title": long_title, "content": "c", "score": 0.9, "source": "s"}]
    service._perform_vector_search = fake_search
    await service._retrieve_documents("query", k=1, filters=None, fusion=False)
    trace_title = service.artifacts["retrieval_trace"][0]["title"]
    assert trace_title.endswith("...")
    assert len(trace_title) == 103  # 100 chars + "..."


# ---------------------------------------------------------------------------
# _rerank_documents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_documents_sorted_and_scored(service):
    docs = [
        {"id": "a", "title": "A", "content": "c", "score": 0.5},
        {"id": "b", "title": "B", "content": "c", "score": 0.9},
        {"id": "c", "title": "C", "content": "c", "score": 0.2},
    ]
    reranked = await service._rerank_documents("q", docs)
    scores = [d["score"] for d in reranked]
    assert scores == sorted(scores, reverse=True)
    # rerank_score attached and scores clamped to [0, 1]
    for d in reranked:
        assert "rerank_score" in d
        assert 0.0 <= d["score"] <= 1.0


# ---------------------------------------------------------------------------
# _generate_answer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("provider,question,marker", [
    ("openai", "What is AI?", "refers to"),
    ("openai", "How to build?", "systematic approach"),
    ("openai", "Why does it fail?", "evidence"),
    ("openai", "Summarize the topic", "comprehensive information"),
])
async def test_generate_answer_branches(service, provider, question, marker):
    docs = [{"id": "d1", "content": "word " * 10}]
    res = await service._generate_answer(question, docs, provider)
    assert marker in res["answer"]
    assert res["answer"].endswith("[1][2][3]")
    assert res["tokens_in"] > 0
    assert res["tokens_out"] > 0
    assert res["provider"] == provider


@pytest.mark.asyncio
async def test_generate_answer_provider_token_differences(service):
    docs = [{"id": "d1", "content": "alpha beta"}]
    openai_res = await service._generate_answer("What is x?", docs, "openai")
    anthropic_res = await service._generate_answer("What is x?", docs, "anthropic")
    other_res = await service._generate_answer("What is x?", docs, "local")
    # The three providers use different base token counts.
    assert openai_res["tokens_out"] == 150
    assert anthropic_res["tokens_out"] == 160
    assert other_res["tokens_out"] == 140


# ---------------------------------------------------------------------------
# _extract_citations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_citations_structure_and_sorting(service):
    docs = [
        {"id": "d1", "title": "Low", "content": "c" * 250, "source": "x.com", "score": 0.3,
         "published_date": "2024-01-01"},
        {"id": "d2", "title": "High", "content": "short", "source": "y.com", "score": 0.9,
         "published_date": "2024-02-01"},
    ]
    citations = await service._extract_citations("answer [1][2]", docs)
    assert len(citations) == 2
    # sorted by relevance_score descending
    assert citations[0]["relevance_score"] >= citations[1]["relevance_score"]
    assert citations[0]["title"] == "High"
    # long excerpt truncated, short excerpt left intact
    low = next(c for c in citations if c["title"] == "Low")
    assert low["excerpt"].endswith("...")
    high = next(c for c in citations if c["title"] == "High")
    assert high["excerpt"] == "short"
    # citation strength clamped to <= 1.0
    assert all(c["citation_strength"] <= 1.0 for c in citations)
    # url built from source + id
    assert "d2" in citations[0]["url"]


@pytest.mark.asyncio
async def test_extract_citations_caps_at_five(service):
    docs = [
        {"id": f"d{i}", "title": f"T{i}", "content": "c", "source": "s.com",
         "score": 0.5, "published_date": "2024-01-01"}
        for i in range(10)
    ]
    citations = await service._extract_citations("answer", docs)
    assert len(citations) == 5


# ---------------------------------------------------------------------------
# Full pipeline via answer_question (mocking mlrun + mlflow)
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_mlflow(monkeypatch):
    """Replace mlrun context manager and the mlflow module used inside answer."""
    import legacy.services.rag.answer as answer_mod

    @contextmanager
    def fake_mlrun(*args, **kwargs):
        yield MagicMock()

    monkeypatch.setattr(answer_mod, "mlrun", fake_mlrun)

    fake_mlflow = MagicMock()
    # answer_question does `import mlflow` inside the function, so patch sys.modules.
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)
    return fake_mlflow


@pytest.mark.asyncio
async def test_answer_question_full_pipeline(service, patched_mlflow):
    response = await service.answer_question(
        "What is retrieval augmented generation?",
        k=3,
        rerank_on=True,
        fusion=True,
    )
    assert response["question"].startswith("What is")
    assert response["answer"].endswith("[1][2][3]")
    assert response["citations"]  # non-empty
    assert response["metadata"]["documents_retrieved"] == 3
    assert response["metadata"]["rerank_enabled"] is True
    assert response["metadata"]["fusion_enabled"] is True
    assert response["metrics"]["k_used"] == 3
    assert response["metrics"]["num_citations"] >= 1
    # mlflow params/metrics were logged
    assert patched_mlflow.log_param.called
    assert patched_mlflow.log_metric.called


@pytest.mark.asyncio
async def test_answer_question_no_rerank_no_fusion(patched_mlflow):
    svc = make_service(rerank_enabled=False, fusion_enabled=False)
    response = await svc.answer_question("Explain photosynthesis", k=2)
    assert response["metadata"]["rerank_enabled"] is False
    assert response["metadata"]["fusion_enabled"] is False
    assert response["metrics"]["rerank_time_ms"] == 0.0
    # fusion disabled -> fusion timing stays zero
    assert response["metrics"]["fusion_time_ms"] == 0.0


@pytest.mark.asyncio
async def test_answer_question_propagates_errors(service, patched_mlflow):
    # Force retrieval to blow up; the method should log error metric and re-raise.
    async def boom(*a, **k):
        raise ValueError("retrieval exploded")
    service._retrieve_documents = boom

    with pytest.raises(ValueError, match="retrieval exploded"):
        await service.answer_question("What breaks?", k=2)
    patched_mlflow.log_metric.assert_any_call("error", 1)
