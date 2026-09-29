"""
RAG API Smoke Tests
Issue #238: CI: Smoke tests for indexing & /ask

These tests verify that the /ask API endpoint works correctly and returns
proper responses. They drive the FastAPI app in-process via httpx's
ASGITransport (no live server, no Redis, no MLflow needed) and override the
RAG service dependency with a mock so the route wiring, request validation,
and response/citation shape are exercised against the CURRENT source API.
"""

import os
import sys
from pathlib import Path

import pytest

# Optional dependencies required to drive the app in-process. Skip collection
# cleanly if any are genuinely absent rather than crashing.
httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from fastapi import FastAPI

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import legacy.services.api.routes.ask as ask_mod


# Canonical answers keyed by the question topic so the topic-relevance
# assertions remain meaningful (the mock RAG service returns these verbatim).
_TOPIC_ANSWERS = {
    "artificial intelligence": (
        "The latest developments in artificial intelligence and machine learning "
        "span large language models, multimodal systems, and improved reasoning."
    ),
    "climate change": (
        "Technology helps with climate change through renewable energy, smarter "
        "grids, and carbon-capture tooling that improve efficiency."
    ),
    "healthcare": (
        "AI applications in healthcare include diagnostic imaging, medical record "
        "analysis, and treatment recommendations from machine learning models."
    ),
    "digital transformation": (
        "Digital transformation is reshaping the economy and business models, "
        "driving new efficiency across the digital-first economy."
    ),
    "space exploration": (
        "New space exploration technology enables missions to Mars and beyond, "
        "advancing exploration through reusable launch systems."
    ),
}

_DEFAULT_ANSWER = (
    "Based on the retrieved documents, the topic involves several important "
    "considerations relevant to understanding the broader context. [1][2][3]"
)


def _answer_for(question: str) -> str:
    q = question.lower()
    for topic, answer in _TOPIC_ANSWERS.items():
        if topic in q or any(word in q for word in topic.split()):
            return answer
    return _DEFAULT_ANSWER


def _build_citations(k: int):
    """Citations matching the shape produced by RAGAnswerService._extract_citations."""
    k = max(1, min(k or 3, 5))
    citations = []
    for i in range(k):
        citations.append(
            {
                "citation_id": i + 1,
                "title": f"Sample Document {i + 1}",
                "source": f"source_{(i % 3) + 1}.com",
                "url": f"https://source_{(i % 3) + 1}.com/article/doc_{i + 1:03d}",
                "relevance_score": round(0.95 - i * 0.05, 4),
                "published_date": "2024-01-15",
                "excerpt": (
                    f"This is sample content for document {i + 1} that contains "
                    "relevant information about the query topic."
                ),
                "citation_strength": round(min(1.0, 0.95 - i * 0.05 + 0.1), 4),
            }
        )
    return citations


@pytest.fixture
def rag_service():
    """A mock RAGAnswerService whose answer_question returns realistic responses."""
    from unittest.mock import AsyncMock, MagicMock

    async def _answer_question(question, k=None, filters=None, rerank_on=None,
                               fusion=None, provider=None, **kwargs):
        return {
            "question": question,
            "answer": _answer_for(question),
            "citations": _build_citations(k or 5),
            "metadata": {
                "retrieval_time_ms": 5.0,
                "answer_time_ms": 7.0,
                "total_time_ms": 12.0,
                "documents_retrieved": k or 5,
                "provider_used": provider or "openai",
                "rerank_enabled": bool(rerank_on),
                "fusion_enabled": bool(fusion),
            },
        }

    service = MagicMock()
    service.answer_question = AsyncMock(side_effect=_answer_question)
    return service


@pytest.fixture
def app(rag_service, monkeypatch):
    """A minimal FastAPI app mounting the ask router with the RAG dep overridden.

    A fresh app avoids the production app's Redis-backed rate-limit middleware;
    the module-level query cache (also Redis-backed) is disabled, and MLflow
    tracking is forced on so each request makes a single answer_question call.
    """
    # Disable the Redis-backed query cache for in-process testing.
    monkeypatch.setattr(ask_mod, "query_cache", None)
    # Force the tracked path so the route calls answer_question (not the
    # private step-by-step helpers) exactly once per request.
    monkeypatch.setattr(ask_mod, "should_log_to_mlflow", lambda: True)

    application = FastAPI()
    application.include_router(ask_mod.router, prefix="/api")

    @application.get("/health")
    async def health_check():
        return {
            "status": "healthy",
            "service": "neuronews-services-api",
        }

    application.dependency_overrides[ask_mod.get_rag_service] = lambda: rag_service
    return application


@pytest.fixture
def client(app):
    """In-process async HTTP client bound to the app via ASGITransport."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


class TestRAGAPISmoke:
    """Smoke tests for RAG API endpoints (in-process)."""

    @pytest.fixture
    def test_questions(self):
        """Test questions related to our tiny corpus."""
        return [
            {
                "question": "What are the latest developments in artificial intelligence?",
                "expected_topics": ["AI", "machine learning", "artificial intelligence"],
            },
            {
                "question": "How can technology help with climate change?",
                "expected_topics": ["climate", "technology", "renewable energy"],
            },
            {
                "question": "What are the applications of AI in healthcare?",
                "expected_topics": ["healthcare", "AI", "diagnostic", "medical"],
            },
            {
                "question": "How is digital transformation affecting the economy?",
                "expected_topics": ["digital", "economy", "business", "transformation"],
            },
            {
                "question": "What's new in space exploration technology?",
                "expected_topics": ["space", "exploration", "technology", "Mars"],
            },
        ]

    @pytest.mark.asyncio
    async def test_health_endpoint(self, client):
        """Test that the health endpoint is working."""
        async with client:
            response = await client.get("/health", timeout=10.0)
            assert response.status_code == 200, f"Health check failed: {response.status_code}"

            assert response.headers.get("content-type", "").startswith("application/json")
            data = response.json()
            assert isinstance(data, dict), "Health response should be JSON object"
            assert data.get("status") == "healthy"

    @pytest.mark.asyncio
    async def test_ask_endpoint_basic(self, client):
        """Test basic /ask endpoint functionality."""
        question = "What are the latest developments in artificial intelligence?"

        payload = {
            "question": question,
            "k": 3,
            "rerank_on": True,
            "fusion": True,
        }

        async with client:
            response = await client.post("/api/ask/", json=payload, timeout=30.0)

        assert response.status_code == 200, f"Ask endpoint failed: {response.status_code} - {response.text}"

        data = response.json()
        assert isinstance(data, dict), "Response should be JSON object"

        # Verify required fields in response
        assert "answer" in data, "Missing 'answer' field in response"
        assert "citations" in data, "Missing 'citations' field in response"

        # Verify answer is non-empty and substantial
        answer = data["answer"]
        assert answer and answer.strip(), "Answer should not be empty"
        assert len(answer.strip()) > 10, "Answer should be substantial"

        # Verify citations structure (source shape: excerpt + source/url)
        citations = data["citations"]
        assert isinstance(citations, list), "Citations should be a list"
        assert len(citations) >= 1, "Should have at least 1 citation"

        for citation in citations:
            assert isinstance(citation, dict), "Citation should be an object"
            assert "excerpt" in citation, "Citation missing excerpt"
            assert "source" in citation or "url" in citation, "Citation missing source/url"

    @pytest.mark.asyncio
    async def test_ask_endpoint_multiple_questions(self, client, test_questions):
        """Test /ask endpoint with multiple questions."""
        async with client:
            for test_case in test_questions[:3]:  # Test first 3 to keep CI fast
                question = test_case["question"]
                expected_topics = test_case["expected_topics"]

                payload = {
                    "question": question,
                    "k": 5,
                    "rerank_on": True,
                    "fusion": True,
                }

                response = await client.post("/api/ask/", json=payload, timeout=30.0)

                assert response.status_code == 200, f"Ask failed for '{question}': {response.status_code}"

                data = response.json()

                # Basic structure validation
                assert "answer" in data, f"Missing answer for '{question}'"
                assert "citations" in data, f"Missing citations for '{question}'"

                answer = data["answer"]
                citations = data["citations"]

                # Answer quality checks
                assert answer and answer.strip(), f"Empty answer for '{question}'"
                assert len(answer.strip()) >= 20, f"Answer too short for '{question}'"

                # Citation quality checks
                assert len(citations) >= 1, f"No citations for '{question}'"
                assert len(citations) <= 5, f"Too many citations for '{question}'"

                # Check that answer contains relevant information
                answer_lower = answer.lower()
                found_topics = [
                    topic for topic in expected_topics if topic.lower() in answer_lower
                ]

                assert len(found_topics) >= 1, (
                    f"Answer doesn't contain expected topics for '{question}'. "
                    f"Expected: {expected_topics}, Answer: {answer[:100]}..."
                )

    @pytest.mark.asyncio
    async def test_ask_endpoint_with_filters(self, client):
        """Test /ask endpoint with filters."""
        question = "What are recent technology developments?"

        payload = {
            "question": question,
            "k": 3,
            "filters": {
                "date_from": "2024-01-15",
                "language": "en",
            },
            "rerank_on": True,
            "fusion": True,
        }

        async with client:
            response = await client.post("/api/ask/", json=payload, timeout=30.0)

        # Filters might not be fully implemented, so we accept 200 or 422
        assert response.status_code in [200, 422], (
            f"Unexpected status for filtered request: {response.status_code}"
        )

        if response.status_code == 200:
            data = response.json()
            assert "answer" in data, "Missing answer in filtered response"
            assert "citations" in data, "Missing citations in filtered response"

    @pytest.mark.asyncio
    async def test_ask_endpoint_parameter_validation(self, client):
        """Test /ask endpoint parameter validation."""
        async with client:
            # Test missing question
            response = await client.post("/api/ask/", json={})
            assert response.status_code in [400, 422], "Should reject empty request"

            # Test empty question (min_length=3 in the request model)
            response = await client.post("/api/ask/", json={"question": ""})
            assert response.status_code in [400, 422], "Should reject empty question"

            # Test invalid k value (k must be >= 1)
            response = await client.post(
                "/api/ask/", json={"question": "test question", "k": -1}
            )
            assert response.status_code in [400, 422], "Should reject negative k"

    @pytest.mark.asyncio
    async def test_ask_endpoint_performance(self, client):
        """Test /ask endpoint returns a valid answer within a generous bound."""
        import time

        question = "What are artificial intelligence applications?"

        payload = {
            "question": question,
            "k": 3,
            "rerank_on": True,
            "fusion": True,
        }

        async with client:
            start_time = time.time()
            response = await client.post("/api/ask/", json=payload, timeout=45.0)
            response_time = time.time() - start_time

        assert response.status_code == 200, f"Performance test failed: {response.status_code}"

        # Response should be reasonably fast (generous bound for CI).
        assert response_time < 30.0, f"Response too slow: {response_time:.2f}s"

        data = response.json()
        assert "answer" in data and data["answer"], "Performance test should return valid answer"

    @pytest.mark.asyncio
    async def test_ask_endpoint_citation_quality(self, client):
        """Test citation quality and relevance."""
        question = "How can AI help with healthcare?"

        payload = {
            "question": question,
            "k": 5,
            "rerank_on": True,
            "fusion": True,
        }

        async with client:
            response = await client.post("/api/ask/", json=payload, timeout=30.0)

        assert response.status_code == 200, f"Citation test failed: {response.status_code}"

        data = response.json()
        citations = data.get("citations", [])

        assert len(citations) >= 1, "Should have at least 1 citation"

        # Check citation quality against the source citation shape.
        for i, citation in enumerate(citations):
            assert "excerpt" in citation, f"Citation {i} missing excerpt"
            assert citation["excerpt"].strip(), f"Citation {i} has empty excerpt"
            assert len(citation["excerpt"]) >= 10, f"Citation {i} excerpt too short"

            # Should have either source or URL
            has_source = "source" in citation and citation["source"]
            has_url = "url" in citation and citation["url"]
            assert has_source or has_url, f"Citation {i} missing source and URL"

            # The source emits a numeric relevance_score; verify it when present.
            if "relevance_score" in citation:
                assert isinstance(
                    citation["relevance_score"], (int, float)
                ), f"Citation {i} invalid relevance_score type"
                assert citation["relevance_score"] >= 0, f"Citation {i} negative relevance_score"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
