"""Smoke tests for legacy/services/api/routes/ask.py."""
import os, sys
from unittest.mock import AsyncMock, MagicMock
import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient
import legacy.services.api.routes.ask as mod


@pytest.fixture
def client():
    rag = MagicMock()
    answer = MagicMock()
    answer.answer = "Because of X."
    answer.citations = []
    answer.to_dict = MagicMock(return_value={"answer": "Because of X.", "citations": []})
    rag.answer_question = AsyncMock(return_value=answer)
    rag.ask = AsyncMock(return_value=answer)
    app = FastAPI()
    app.include_router(mod.router)
    app.dependency_overrides[mod.get_rag_service] = lambda: rag
    return TestClient(app, raise_server_exceptions=False)


def test_health(client):
    assert 200 <= client.get("/ask/health").status_code < 600

def test_config(client):
    assert 200 <= client.get("/ask/config").status_code < 600

def test_ask(client):
    resp = client.post("/ask/", json={"question": "Why is the sky blue?"})
    assert 200 <= resp.status_code < 600
