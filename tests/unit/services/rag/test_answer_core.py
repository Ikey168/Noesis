"""Tests for core logic of legacy/services/rag/answer.py."""

import os
import sys
from unittest.mock import MagicMock

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from legacy.services.rag.answer import RAGAnswerService  # noqa: E402


@pytest.fixture
def service():
    # pass a mock embeddings provider to avoid loading a real model
    return RAGAnswerService(embeddings_provider=MagicMock(), default_k=5)


def docs(n=3):
    return [
        {"id": f"d{i}", "title": f"Title {i}", "content": "x" * (300 if i == 0 else 50),
         "source": "bbc.com", "score": 0.9 - i * 0.1, "published_date": f"2026-01-1{i}"}
        for i in range(n)
    ]


class TestInit:
    def test_defaults(self, service):
        assert service.default_k == 5
        assert service.rerank_enabled is True
        assert service.metrics["num_citations"] == 0

    def test_custom(self):
        s = RAGAnswerService(embeddings_provider=MagicMock(), default_k=3,
                             rerank_enabled=False, answer_provider="anthropic")
        assert s.default_k == 3
        assert s.rerank_enabled is False
        assert s.answer_provider == "anthropic"


class TestClassifyQuestionType:
    @pytest.mark.parametrize("q,expected", [
        ("What is AI?", "factual"),
        ("Which model is best?", "factual"),
        ("How does it work?", "explanatory"),
        ("Why is the sky blue?", "explanatory"),
        ("When did it happen?", "temporal_spatial"),
        ("Where is it located?", "temporal_spatial"),
        ("Who invented it?", "entity"),
        ("Tell me about it?", "general_question"),
        ("Summarize the news", "statement_query"),
    ])
    def test_classification(self, service, q, expected):
        assert service._classify_question_type(q) == expected


class TestExtractCitations:
    @pytest.mark.asyncio
    async def test_extracts_and_sorts(self, service):
        citations = await service._extract_citations("answer text", docs(3))
        assert len(citations) == 3
        # sorted by relevance descending
        scores = [c["relevance_score"] for c in citations]
        assert scores == sorted(scores, reverse=True)
        assert citations[0]["citation_id"] in (1, 2, 3)

    @pytest.mark.asyncio
    async def test_excerpt_truncation(self, service):
        citations = await service._extract_citations("a", docs(1))
        # first doc content is 300 chars -> excerpt truncated with ...
        assert citations[0]["excerpt"].endswith("...")

    @pytest.mark.asyncio
    async def test_caps_at_five(self, service):
        citations = await service._extract_citations("a", docs(8))
        assert len(citations) == 5

    @pytest.mark.asyncio
    async def test_empty_docs(self, service):
        assert await service._extract_citations("a", []) == []


class TestMetrics:
    def test_get_metrics_returns_copy(self, service):
        m = service.get_metrics()
        m["k_used"] = 99
        assert service.metrics["k_used"] == 0  # original unchanged

    def test_reset_metrics(self, service):
        service.metrics["tokens_in"] = 100
        service.artifacts["citations"] = [{"x": 1}]
        service._reset_metrics()
        assert service.metrics["tokens_in"] == 0
        assert service.artifacts["citations"] == []
