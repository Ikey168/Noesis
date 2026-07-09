"""Unit tests for the persisted enrichment sink (#908). Offline, in-memory DuckDB."""

from __future__ import annotations

import duckdb
import pytest

from src.ingestion.enrichment_store import EnrichmentStore


@pytest.fixture
def store() -> EnrichmentStore:
    return EnrichmentStore(duckdb.connect(":memory:"))


def test_ensure_schema_is_idempotent():
    conn = duckdb.connect(":memory:")
    EnrichmentStore(conn)
    EnrichmentStore(conn)  # second construction must not raise
    assert conn.execute("SELECT COUNT(*) FROM document_enrichments").fetchone()[0] == 0


def test_upsert_and_get_roundtrip(store: EnrichmentStore):
    store.upsert(
        "d1", sentiment_score=0.8, sentiment_label="positive",
        topics=["energy", "policy"], updated_at=1_700_000_000_000,
    )
    rec = store.get("d1")
    assert rec == {
        "sentiment_score": 0.8,
        "sentiment_label": "positive",
        "topics": ["energy", "policy"],
        "updated_at": 1_700_000_000_000,
    }


def test_get_missing_returns_none(store: EnrichmentStore):
    assert store.get("nope") is None


def test_upsert_is_idempotent_last_write_wins(store: EnrichmentStore):
    store.upsert("d1", sentiment_score=0.1, sentiment_label="negative", topics=["a"])
    store.upsert("d1", sentiment_score=0.9, sentiment_label="positive", topics=["b", "c"])
    rec = store.get("d1")
    assert rec["sentiment_score"] == 0.9
    assert rec["sentiment_label"] == "positive"
    assert rec["topics"] == ["b", "c"]
    assert store.count() == 1  # replaced, not duplicated


def test_partial_enrichment_stores_nulls(store: EnrichmentStore):
    store.upsert("d1", sentiment_score=0.5)  # no label, no topics
    rec = store.get("d1")
    assert rec["sentiment_score"] == 0.5
    assert rec["sentiment_label"] is None
    assert rec["topics"] == []


def test_upsert_enrichments_from_dict(store: EnrichmentStore):
    store.upsert_enrichments("d1", {"sentiment_score": -0.3, "topics": ["climate"]})
    rec = store.get("d1")
    assert rec["sentiment_score"] == -0.3
    assert rec["topics"] == ["climate"]
    assert rec["sentiment_label"] is None


def test_multiple_documents_are_independent(store: EnrichmentStore):
    store.upsert("d1", sentiment_label="positive")
    store.upsert("d2", sentiment_label="negative")
    assert store.get("d1")["sentiment_label"] == "positive"
    assert store.get("d2")["sentiment_label"] == "negative"
    assert store.count() == 2
