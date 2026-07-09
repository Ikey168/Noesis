"""
Document sink for the Kafka ingest consumer (#916).

The Kafka ingest consumer historically validated ``article-ingest-v1`` and had
no sink — valid messages were validated and dropped. This module is the sink:
a small, Kafka-free core that normalizes an incoming payload to
``document-ingest-v1`` (bridging legacy ``article-ingest-v1`` messages) and
persists it through a :class:`~src.ingestion.document_store.DocumentStore`.

Kept import-free of ``confluent_kafka`` so it is offline-testable and importable
in the CI gate; the Kafka wiring lives in :mod:`services.ingest.consumer`.
"""

from __future__ import annotations

from typing import Any, Dict

from services.ingest.common.document_model import article_to_document


def to_document_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an incoming payload to a ``document-ingest-v1`` dict.

    A payload that already has a ``document_id`` is passed through. A legacy
    ``article-ingest-v1`` payload (``article_id``) is bridged via
    :func:`article_to_document`. Anything else is returned unchanged so
    downstream validation rejects it (rather than raising here).
    """
    if "document_id" in payload:
        return payload
    if "article_id" in payload:
        try:
            return article_to_document(payload)
        except Exception:  # noqa: BLE001 - malformed article -> let validation reject
            return payload
    return payload


class DocumentSink:
    """Validate-and-persist sink: one payload -> ``documents`` (or dead-letter).

    Wraps a :class:`DocumentStore`; ``__call__`` normalizes, validates (via the
    store's ``document-ingest-v1`` validation), and upserts, returning a small
    outcome dict (``stored`` / ``duplicate`` / ``invalid``). Never raises on a
    bad payload — an invalid document is reported, not thrown, so the caller can
    route it to a DLQ. Running counters (``stored``/``duplicate``/``invalid``)
    are kept for metrics.
    """

    def __init__(self, store, validate: bool = True):
        self.store = store
        self.validate = validate
        self.stored = 0
        self.duplicate = 0
        self.invalid = 0

    def __call__(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        doc = to_document_payload(payload)
        summary = self.store.upsert([doc], validate=self.validate)
        document_id = doc.get("document_id")
        if summary.invalid:
            self.invalid += 1
            error = summary.dead_letter[0]["error"] if summary.dead_letter else "invalid document"
            return {"outcome": "invalid", "document_id": document_id, "error": error}
        if summary.inserted:
            self.stored += 1
            return {"outcome": "stored", "document_id": document_id}
        self.duplicate += 1
        return {"outcome": "duplicate", "document_id": document_id}

    def metrics(self) -> Dict[str, int]:
        return {"stored": self.stored, "duplicate": self.duplicate, "invalid": self.invalid}
