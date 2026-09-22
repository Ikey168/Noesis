"""Tests for legacy/services/api/middleware/metrics.py."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("fastapi")
pytest.importorskip("prometheus_client")

from legacy.services.api.middleware.metrics import (  # noqa: E402
    RAGMetricsMiddleware,
    add_metrics_middleware,
    create_metrics_endpoint,
    setup_rag_observability,
    track_rag_request,
)


@pytest.fixture
def mw():
    return RAGMetricsMiddleware(app=MagicMock(), track_all_endpoints=False)


class TestIsRagEndpoint:
    @pytest.mark.parametrize("path", ["/ask", "/search", "/retrieve", "/answer"])
    def test_rag_paths(self, mw, path):
        assert mw._is_rag_endpoint(path) is True

    def test_non_rag_path(self, mw):
        assert mw._is_rag_endpoint("/health") is False


class TestComponentFromPath:
    @pytest.mark.parametrize("path,component", [
        ("/ask", "answer"),
        ("/answer", "answer"),
        ("/search", "retrieval"),
        ("/retrieve", "retrieval"),
        ("/rerank", "rerank"),
        ("/other", "api"),
    ])
    def test_component(self, mw, path, component):
        assert mw._determine_component_from_path(path) == component


class TestDispatch:
    @pytest.mark.asyncio
    async def test_skips_non_rag_when_not_tracking_all(self, mw):
        request = MagicMock()
        request.method = "GET"
        request.url.path = "/health"
        expected = MagicMock()
        call_next = AsyncMock(return_value=expected)
        result = await mw.dispatch(request, call_next)
        assert result is expected
        call_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tracks_rag_endpoint(self):
        mw = RAGMetricsMiddleware(app=MagicMock(), track_all_endpoints=False)
        request = MagicMock()
        request.method = "POST"
        request.url.path = "/ask"
        request.query_params = {}
        response = MagicMock()
        response.status_code = 200
        call_next = AsyncMock(return_value=response)
        result = await mw.dispatch(request, call_next)
        assert result is response

    @pytest.mark.asyncio
    async def test_track_all_endpoints(self):
        mw = RAGMetricsMiddleware(app=MagicMock(), track_all_endpoints=True)
        request = MagicMock()
        request.method = "GET"
        request.url.path = "/health"
        request.query_params = {}
        response = MagicMock()
        response.status_code = 200
        call_next = AsyncMock(return_value=response)
        result = await mw.dispatch(request, call_next)
        assert result is response


class TestTrackRagRequest:
    def test_context_manager(self):
        with track_rag_request(provider="openai", operation="ask") as ctx:
            assert ctx.provider == "openai"
            assert ctx.operation == "ask"
            assert ctx.start_time is not None

    def test_defaults(self):
        ctx = track_rag_request(provider="anthropic", operation="search")
        assert ctx.lang == "en"
        assert ctx.has_rerank is False


class TestIntegrationHelpers:
    def test_add_metrics_middleware(self):
        from fastapi import FastAPI
        app = FastAPI()
        add_metrics_middleware(app, track_all_endpoints=True)
        # middleware registered without raising

    def test_create_metrics_endpoint(self):
        from fastapi import FastAPI
        app = FastAPI()
        create_metrics_endpoint(app, "/metrics")
        paths = [r.path for r in app.routes]
        assert "/metrics" in paths

    def test_setup_rag_observability(self):
        from fastapi import FastAPI
        app = FastAPI()
        setup_rag_observability(app)
        paths = [r.path for r in app.routes]
        assert "/metrics" in paths
