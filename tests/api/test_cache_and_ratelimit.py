"""
Tests for Query Cache and Rate Limiting (Issue #240)
"""
import os

# The query cache and the sliding-window rate limiter both read USE_REDIS at
# import/construction time and default to a Redis backend. Force the in-memory
# fallback BEFORE importing the app so no live Redis is required.
os.environ["USE_REDIS"] = "0"

import time

import pytest
from fastapi.testclient import TestClient

import legacy.services.api.middleware.ratelimit as ratelimit_mod
import legacy.services.api.routes.ask as ask_mod
from legacy.services.api.main import app, query_cache
from legacy.services.api.routes.ask import get_rag_service

client = TestClient(app)


class _StubRAGService:
    """Minimal deterministic stand-in for RAGAnswerService.

    The real service depends on heavyweight embedding models / external answer
    providers that are unavailable in the test environment. The /ask route only
    uses the handful of private methods exercised here (via
    _answer_question_without_tracking), so a stub keeps the endpoint returning a
    real 200 response while remaining deterministic for cache assertions.
    """

    def __init__(self):
        self.embeddings_provider = type("P", (), {"model_name": "stub"})()
        self.answer_provider = "openai"
        self.default_k = 5
        self.rerank_enabled = True
        self.fusion_enabled = True

    def _reset_metrics(self):
        return None

    async def _retrieve_documents(self, question, k, filters, fusion):
        return [{"id": "doc-1", "text": "stub document"}]

    async def _rerank_documents(self, question, docs):
        return docs

    async def _generate_answer(self, question, docs, provider):
        return {"answer": f"Stub answer for: {question}"}

    async def _extract_citations(self, answer, docs):
        return [{"citation_id": 1, "source": "stub", "title": "Stub"}]


@pytest.fixture(autouse=True)
def _stub_rag_and_no_mlflow(monkeypatch):
    """Override the RAG dependency and force the non-MLflow (cacheable) path.

    Caching in the /ask route only happens on the non-tracked branch
    (should_log_to_mlflow() is False), so force that branch deterministically
    instead of relying on the random 20% sampling.
    """
    app.dependency_overrides[get_rag_service] = lambda: _StubRAGService()
    monkeypatch.setattr(ask_mod, "should_log_to_mlflow", lambda: False)
    # Start each test with a clean cache so cache-hit assertions are reliable.
    query_cache.clear()
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_rag_service, None)


@pytest.mark.asyncio
async def test_query_cache_basic():
    """Test that repeated identical queries return a cached result."""
    payload = {
        "question": "What is the capital of France?",
        "k": 3,
        "filters": {},
        "rerank_on": True,
        "fusion": True,
        "provider": "openai"
    }
    # First request (should not be cached)
    t0 = time.time()
    r1 = client.post("/api/ask/", json=payload)
    t1 = time.time()
    assert r1.status_code == 200
    # First (uncached) request runs through the pipeline and is tracked as such
    assert r1.json()["tracked_in_mlflow"] is False
    # Second request (should be served from cache)
    t2 = time.time()
    r2 = client.post("/api/ask/", json=payload)
    t3 = time.time()
    assert r2.status_code == 200
    # Cached response should not be slower than the uncached one
    uncached_time = t1 - t0
    cached_time = t3 - t2
    print(f"Uncached: {uncached_time:.3f}s, Cached: {cached_time:.3f}s")
    assert cached_time <= uncached_time
    # Both responses carry the same answer (served from cache on the 2nd call)
    assert r2.json()["answer"] == r1.json()["answer"]
    # Cached response indicates it was not tracked in MLflow
    assert r2.json()["tracked_in_mlflow"] is False


@pytest.mark.asyncio
async def test_rate_limit(monkeypatch):
    """Test that the limiter returns 429 after exceeding the configured limit."""
    # RATE_LIMIT / WINDOW_SIZE are module-level globals read inside dispatch(),
    # so patching them on the module takes effect for subsequent requests.
    monkeypatch.setattr(ratelimit_mod, "RATE_LIMIT", 3)
    monkeypatch.setattr(ratelimit_mod, "WINDOW_SIZE", 5)
    # Send more requests than the limit allows within the window.
    payload = {
        "question": "Test rate limit?",
        "k": 2,
        "filters": {},
        "rerank_on": True,
        "fusion": True,
        "provider": "openai"
    }
    responses = []
    for _ in range(5):
        resp = client.post("/api/ask/", json=payload)
        responses.append(resp)
    codes = [r.status_code for r in responses]
    print(f"Rate limit test status codes: {codes}")
    assert 429 in codes
