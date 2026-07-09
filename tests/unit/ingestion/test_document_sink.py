"""Unit tests for the Kafka consumer's document sink core (#916). Offline."""

from __future__ import annotations

import duckdb
import pytest

from services.ingest.document_sink import DocumentSink, to_document_payload
from src.ingestion.document_store import DocumentStore


@pytest.fixture
def sink() -> DocumentSink:
    return DocumentSink(DocumentStore(duckdb.connect(":memory:")))


def _doc_payload(doc_id="d1", source_type="news", content="Body."):
    return {
        "document_id": doc_id,
        "source_type": source_type,
        "language": "en",
        "ingested_at": 1_700_000_000_000,
        "url": f"https://ex.com/{doc_id}",
        "content": content,
    }


def _article_payload(article_id="a1"):
    return {
        "article_id": article_id,
        "language": "en",
        "ingested_at": 1_700_000_000_000,
        "url": f"https://ex.com/{article_id}",
        "title": "Headline",
        "body": "Article body.",
        "source_id": "reuters",
        "published_at": 1_699_000_000_000,
        "country": "US",
    }


# --------------------------------------------------------------------------- #
# to_document_payload
# --------------------------------------------------------------------------- #


def test_document_payload_passes_through():
    p = _doc_payload()
    assert to_document_payload(p) is p


def test_article_payload_is_bridged_to_document():
    doc = to_document_payload(_article_payload("a1"))
    assert doc["document_id"] == "a1"
    assert doc["source_type"] == "news"
    assert doc["content"] == "Article body."
    assert doc["metadata"].get("country") == "US"


def test_malformed_article_returned_unchanged_for_rejection():
    # Missing required article fields -> not bridgeable -> returned as-is.
    p = {"article_id": "a1"}
    assert to_document_payload(p) == p


# --------------------------------------------------------------------------- #
# DocumentSink
# --------------------------------------------------------------------------- #


def test_valid_document_is_stored(sink: DocumentSink):
    res = sink(_doc_payload("d1"))
    assert res == {"outcome": "stored", "document_id": "d1"}
    assert sink.store.count() == 1


def test_duplicate_document_is_reported(sink: DocumentSink):
    sink(_doc_payload("d1"))
    res = sink(_doc_payload("d1"))
    assert res["outcome"] == "duplicate"
    assert sink.store.count() == 1


def test_invalid_document_is_dead_lettered_not_raised(sink: DocumentSink):
    bad = _doc_payload("d1")
    bad["source_type"] = "not-a-type"
    res = sink(bad)
    assert res["outcome"] == "invalid"
    assert res["document_id"] == "d1"
    assert "error" in res
    assert sink.store.count() == 0


def test_missing_required_field_is_invalid(sink: DocumentSink):
    bad = _doc_payload("d1")
    del bad["language"]
    assert sink(bad)["outcome"] == "invalid"


def test_article_payload_is_bridged_and_stored(sink: DocumentSink):
    res = sink(_article_payload("a1"))
    assert res == {"outcome": "stored", "document_id": "a1"}
    stored = sink.store.get("a1")
    assert stored["source_type"] == "news"


def test_metrics_track_outcomes(sink: DocumentSink):
    sink(_doc_payload("d1"))
    sink(_doc_payload("d1"))  # duplicate
    bad = _doc_payload("d2"); bad["source_type"] = "x"
    sink(bad)  # invalid
    assert sink.metrics() == {"stored": 1, "duplicate": 1, "invalid": 1}
