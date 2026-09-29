#!/usr/bin/env python3
"""Import smoke tests for the evaluation-framework modules (Issue #235).

Each test asserts that a public entry point imports and resolves to a real
object under its current module path. Modules whose hard dependency (mlflow)
is not installed are skipped via ``pytest.importorskip`` -- an absent package
is treated the same way an absent external service would be.
"""

import sys
from pathlib import Path

import pytest

# Repo root is two levels up from this file: <repo>/tests/unit/<file>
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_embedding_provider_import():
    """services.embeddings.provider.EmbeddingProvider imports."""
    from services.embeddings.provider import EmbeddingProvider

    assert isinstance(EmbeddingProvider, type)


def test_rag_answer_service_import():
    """legacy.services.rag.answer.RAGAnswerService imports (requires mlflow)."""
    pytest.importorskip("mlflow", reason="RAG answer service requires mlflow")

    from legacy.services.rag.answer import RAGAnswerService

    assert isinstance(RAGAnswerService, type)


def test_mlflow_tracking_import():
    """services.mlops.tracking.mlrun imports (requires mlflow)."""
    pytest.importorskip("mlflow", reason="mlops tracking requires mlflow")

    from services.mlops.tracking import mlrun

    assert callable(mlrun)


def test_api_ask_routes_import():
    """legacy.services.api.routes.ask public symbols import."""
    from legacy.services.api.routes.ask import AskRequest, ask_question, get_rag_service

    assert isinstance(AskRequest, type)
    assert callable(ask_question)
    assert callable(get_rag_service)
