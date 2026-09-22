"""
In-process tests for the FastAPI ``/ask`` route.

The route lives in ``legacy.services.api.routes.ask`` and depends on a RAG service
obtained via the ``get_rag_service`` dependency. These tests build a small
FastAPI app that mounts the real router, override ``get_rag_service`` with an
``AsyncMock`` so no real embedding model / LLM is required, and drive the app
in-process with ``httpx.ASGITransport`` (no network, no live server).

Each test sets up its own mocks and cleans up dependency overrides so the file
passes in isolation and does not leak state between tests.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import httpx
    from httpx import ASGITransport
    from fastapi import FastAPI
    from legacy.services.api.routes.ask import router as ask_router
    from legacy.services.api.routes import ask as ask_module
except ImportError as _e:  # stale or optional dependency
    pytest.skip("module import failed: {0}".format(_e), allow_module_level=True)


def _make_app():
    """Build a fresh app mounting the real ask router."""
    app = FastAPI(title="NeuroNews Ask API Test", version="1.0.0")
    app.include_router(ask_router)
    return app


def _rag_response(question="What is AI?"):
    """A well-formed RAGAnswerService.answer_question return value."""
    return {
        "question": question,
        "answer": "AI is the simulation of human intelligence by machines.",
        "citations": [
            {
                "citation_id": 1,
                "title": "Intro to AI",
                "source": "example.com",
                "url": "https://example.com/ai",
                "relevance_score": 0.91,
                "published_date": "2026-01-01",
                "excerpt": "Artificial intelligence ...",
                "citation_strength": 0.8,
            }
        ],
        "metadata": {
            "retrieval_time_ms": 5.0,
            "answer_time_ms": 10.0,
            "rerank_time_ms": 1.0,
            "total_time_ms": 16.0,
            "documents_retrieved": 1,
            "provider_used": "openai",
            "rerank_enabled": True,
            "fusion_enabled": True,
            "sampling_status": "tracked",
        },
    }


@pytest.fixture
def mock_rag_service():
    """AsyncMock standing in for RAGAnswerService."""
    service = MagicMock()
    service.answer_question = AsyncMock(return_value=_rag_response())

    # Attributes/methods used by /health and /config and the non-tracked path.
    service.default_k = 5
    service.rerank_enabled = True
    service.fusion_enabled = True
    service.answer_provider = "openai"
    embeddings = MagicMock()
    embeddings.model_name = "all-MiniLM-L6-v2"
    embeddings.embedding_dimension = 384
    service.embeddings_provider = embeddings

    service._reset_metrics = MagicMock()
    service._retrieve_documents = AsyncMock(return_value=[{"id": "d1"}])
    service._rerank_documents = AsyncMock(return_value=[{"id": "d1"}])
    service._generate_answer = AsyncMock(return_value={"answer": "AI answer"})
    service._extract_citations = AsyncMock(return_value=_rag_response()["citations"])
    return service


@pytest.fixture
def client(mock_rag_service):
    """httpx AsyncClient bound to the app with get_rag_service overridden.

    Also disables the module-level query cache so cached results from one test
    cannot leak into another.
    """
    app = _make_app()
    app.dependency_overrides[ask_module.get_rag_service] = lambda: mock_rag_service

    saved_cache = ask_module.query_cache
    ask_module.query_cache = None
    transport = ASGITransport(app=app)
    async_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    try:
        yield async_client
    finally:
        # Cleanup is handled per-test via anyio; ensure overrides/cache restored.
        app.dependency_overrides.clear()
        ask_module.query_cache = saved_cache


@pytest.mark.asyncio
async def test_ask_tracked_path(client, mock_rag_service):
    """When sampling selects tracking, answer_question is used directly."""
    with patch.object(ask_module, "should_log_to_mlflow", return_value=True):
        async with client as ac:
            resp = await ac.post("/ask/", json={"question": "What is AI?", "k": 3})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["question"] == "What is AI?"
    assert data["answer"] == _rag_response()["answer"]
    assert data["tracked_in_mlflow"] is True
    assert data["request_id"].startswith("ask_")
    assert len(data["citations"]) == 1
    mock_rag_service.answer_question.assert_awaited_once()


@pytest.mark.asyncio
async def test_ask_untracked_path(client, mock_rag_service):
    """When sampling skips tracking, the manual pipeline is exercised."""
    with patch.object(ask_module, "should_log_to_mlflow", return_value=False):
        async with client as ac:
            resp = await ac.post(
                "/ask/", json={"question": "Explain retrieval augmented generation"}
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tracked_in_mlflow"] is False
    # answer_question must NOT be called on the untracked path.
    mock_rag_service.answer_question.assert_not_awaited()
    mock_rag_service._retrieve_documents.assert_awaited_once()
    mock_rag_service._generate_answer.assert_awaited_once()
    assert data["answer"] == "AI answer"


@pytest.mark.asyncio
async def test_ask_validation_error_short_question(client):
    """Questions shorter than the min length are rejected by FastAPI (422)."""
    async with client as ac:
        resp = await ac.post("/ask/", json={"question": "ab"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ask_service_failure_returns_500(client, mock_rag_service):
    """A failure inside the RAG service is surfaced as HTTP 500."""
    mock_rag_service.answer_question.side_effect = RuntimeError("boom")
    with patch.object(ask_module, "should_log_to_mlflow", return_value=True):
        async with client as ac:
            resp = await ac.post("/ask/", json={"question": "trigger failure"})
    assert resp.status_code == 500
    assert "Failed to process question" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ask_health(client, mock_rag_service):
    """The /ask/health endpoint reports the injected service's config."""
    # /health and /config call get_rag_service() directly (not via Depends),
    # so the dependency override does not cover them -- patch the function.
    with patch.object(ask_module, "get_rag_service", return_value=mock_rag_service):
        async with client as ac:
            resp = await ac.get("/ask/health")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["embedding_model"] == "all-MiniLM-L6-v2"
    assert data["default_provider"] == "openai"


@pytest.mark.asyncio
async def test_ask_config(client, mock_rag_service):
    """The /ask/config endpoint returns retrieval/generation configuration."""
    with patch.object(ask_module, "get_rag_service", return_value=mock_rag_service):
        async with client as ac:
            resp = await ac.get("/ask/config")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["retrieval"]["default_k"] == 5
    assert data["retrieval"]["embedding_model"] == "all-MiniLM-L6-v2"
    assert data["generation"]["default_provider"] == "openai"


@pytest.mark.asyncio
async def test_ask_batch(client, mock_rag_service):
    """The /ask/batch endpoint processes multiple questions."""
    with patch.object(ask_module, "should_log_to_mlflow", return_value=True):
        async with client as ac:
            resp = await ac.post(
                "/ask/batch",
                json={"questions": ["First question here", "Second question here"]},
            )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_questions"] == 2
    assert len(data["results"]) == 2
    assert mock_rag_service.answer_question.await_count == 2
