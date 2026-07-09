"""Routing tests for DocumentIngestConsumer.process_message (#916).

Offline: the consumer is built without __init__ (so no Kafka client is needed),
with a fake message and a fake DLQ producer, to exercise the deserialize ->
sink -> DLQ routing.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import duckdb
import pytest

from services.ingest.consumer import DocumentIngestConsumer
from services.ingest.document_sink import DocumentSink
from src.ingestion.document_store import DocumentStore


class _FakeMessage:
    def __init__(self, payload: dict):
        self._value = json.dumps(payload).encode("utf-8")

    def value(self):
        return self._value

    def topic(self):
        return "document_ingest"

    def partition(self):
        return 0

    def offset(self):
        return 1

    def timestamp(self):
        return (0, 1_700_000_000_000)


def _consumer():
    """A DocumentIngestConsumer wired to a real in-memory sink but no Kafka."""
    c = DocumentIngestConsumer.__new__(DocumentIngestConsumer)
    c._sink = DocumentSink(DocumentStore(duckdb.connect(":memory:")))
    c.avro_deserializer = None
    c.processed_count = 0
    c.dlq_count = 0
    c.dlq_topic = "document_ingest_dlq"
    c.schema_version = "v1"
    c.consumer_group = "document-ingest-consumer"
    c.metrics = MagicMock()
    c.dlq_producer = MagicMock()
    return c


def _doc(doc_id="d1", source_type="news"):
    return {
        "document_id": doc_id, "source_type": source_type, "language": "en",
        "ingested_at": 1_700_000_000_000, "url": f"https://ex.com/{doc_id}",
        "content": "Body.",
    }


def test_valid_message_is_stored_and_callback_fires():
    c = _consumer()
    seen = []
    ok = c.process_message(_FakeMessage(_doc("d1")), callback=seen.append)
    assert ok is True
    assert c._sink.store.count() == 1
    assert seen and seen[0]["document_id"] == "d1"
    c.dlq_producer.produce.assert_not_called()


def test_invalid_message_goes_to_dlq_keyed_by_document_id():
    c = _consumer()
    bad = _doc("d1", source_type="not-a-type")
    ok = c.process_message(_FakeMessage(bad))
    assert ok is False
    assert c._sink.store.count() == 0
    c.dlq_producer.produce.assert_called_once()
    # DLQ key is the document_id, not article_id.
    assert c.dlq_producer.produce.call_args.kwargs["key"] == b"d1"


def test_article_message_is_bridged_and_stored():
    c = _consumer()
    article = {
        "article_id": "a1", "language": "en", "ingested_at": 1_700_000_000_000,
        "url": "https://ex.com/a1", "title": "H", "body": "Body.",
        "source_id": "reuters", "published_at": 1_699_000_000_000,
    }
    ok = c.process_message(_FakeMessage(article))
    assert ok is True
    stored = c._sink.store.get("a1")
    assert stored is not None and stored["source_type"] == "news"


def test_duplicate_message_is_not_dlqd():
    c = _consumer()
    c.process_message(_FakeMessage(_doc("d1")))
    ok = c.process_message(_FakeMessage(_doc("d1")))
    assert ok is True  # duplicate is a success, not a DLQ
    c.dlq_producer.produce.assert_not_called()
    assert c._sink.store.count() == 1
