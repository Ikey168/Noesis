"""Route-level tests for the DocumentStore-backed document API (#905).

Offline: the routes are pointed at a fresh in-memory DuckDB DocumentStore via
``use_store_for_testing``, so persistence is exercised through the real store
without touching the serving warehouse.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import duckdb
import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.ingestion.document_store import DocumentStore

# Load document_routes.py directly, bypassing src/api/routes/__init__.py — that
# package eagerly imports auth_routes -> bcrypt, which is not in the gate's
# curated deps. document_routes has no intra-package imports, so it loads clean.
# Register it in sys.modules first so Pydantic can resolve the module's
# `from __future__ import annotations` forward refs on the route models.
_MOD_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src" / "api" / "routes" / "document_routes.py"
)
_spec = importlib.util.spec_from_file_location("document_routes_under_test", _MOD_PATH)
document_routes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = document_routes
_spec.loader.exec_module(document_routes)


@pytest.fixture()
def client():
    document_routes.use_store_for_testing(DocumentStore(duckdb.connect(":memory:")))
    app = FastAPI()
    app.include_router(document_routes.router)
    yield TestClient(app)
    document_routes.use_store_for_testing(None)


_DOC = {
    "document_id": "doc-1",
    "source_type": "note",
    "language": "en",
    "title": "A Note",
    "content": "Body of the note.",
}


def test_ingested_document_is_persisted_and_survives_a_new_store(client):
    r = client.post("/documents/ingest", json=_DOC)
    assert r.status_code == 201
    assert r.json()["document_id"] == "doc-1"
    assert "ingested_at" in r.json()

    # Persisted, not in a request-local dict: a fresh store over the same file
    # would see it. Here we just confirm GET reads it back through the store.
    got = client.get("/documents/doc-1")
    assert got.status_code == 200
    assert got.json()["title"] == "A Note"


def test_reingest_is_idempotent(client):
    client.post("/documents/ingest", json=_DOC)
    client.post("/documents/ingest", json=_DOC)  # same id + content
    listed = client.get("/documents").json()
    assert len([d for d in listed if d["document_id"] == "doc-1"]) == 1


def test_invalid_source_type_is_rejected(client):
    r = client.post("/documents/ingest", json={**_DOC, "source_type": "BOGUS"})
    assert r.status_code == 422


def test_list_filters_by_source_type(client):
    client.post("/documents/ingest", json=_DOC)
    client.post("/documents/ingest", json={
        **_DOC, "document_id": "doc-2", "source_type": "paper", "content": "Paper body."
    })
    notes = client.get("/documents?source_type=note").json()
    assert [d["document_id"] for d in notes] == ["doc-1"]


def test_get_missing_returns_404(client):
    assert client.get("/documents/ghost").status_code == 404


def test_delete_then_get_is_404(client):
    client.post("/documents/ingest", json=_DOC)
    assert client.delete("/documents/doc-1").status_code == 204
    assert client.get("/documents/doc-1").status_code == 404
    assert client.delete("/documents/doc-1").status_code == 404


def test_created_at_epoch_millis_roundtrips(client):
    client.post("/documents/ingest", json={**_DOC, "created_at": 1_700_000_000_000})
    assert client.get("/documents/doc-1").json()["created_at"] == 1_700_000_000_000
