"""
Unified, validated, deduped document sink (#894, #893, #897).

Persistence used to be fragmented — ``news_articles`` (exact-URL dedup),
``dataset_observations`` (vintage upsert), ``assets`` (sha256), per-connector
``ingest_to_kg`` — with **no** ``documents`` table and no single place that
validates against the contract, dedups, or makes ingestion idempotent.

``DocumentStore`` is that place. ``upsert(documents)``:

- validates each document against ``document-ingest-v1`` (#893) — invalid ones
  are dead-lettered (returned + logged), never written;
- canonicalizes the URL and computes a content hash (#895) — unchanged
  observation identities are idempotent; syndicated observations remain distinct;
- appends immutable revisions for changed identities, then advances the
  compatibility ``documents`` projection;

and returns an :class:`UpsertSummary` (#897). The DuckDB connection is injected,
so the store is offline-testable against an in-memory database.
"""

from __future__ import annotations

import json
import hashlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from services.ingest.common.document_model import Document

logger = logging.getLogger(__name__)

# A validator raises on an invalid payload; the default uses document-ingest-v1.
Validator = Callable[[dict[str, Any]], None]

# Columns of the documents table, in schema order.
_COLUMNS = (
    "document_id",
    "source_type",
    "language",
    "ingested_at",
    "created_at",
    "source_id",
    "url",
    "canonical_url",
    "content_hash",
    "title",
    "content",
    "content_ref",
    "authors",
    "metadata",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id   TEXT PRIMARY KEY,
    source_type   TEXT NOT NULL,
    language      TEXT,
    ingested_at   BIGINT,
    created_at    BIGINT,
    source_id     TEXT,
    url           TEXT,
    canonical_url TEXT,
    content_hash  TEXT,
    title         TEXT,
    content       TEXT,
    content_ref   TEXT,
    authors       TEXT,   -- JSON array
    metadata      TEXT    -- JSON object
)
"""


@dataclass
class UpsertSummary:
    """Outcome of one :meth:`DocumentStore.upsert` call (#897)."""

    received: int = 0
    inserted: int = 0
    updated: int = 0
    retracted: int = 0
    deleted: int = 0
    duplicate: int = 0
    invalid: int = 0
    dead_letter: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "inserted": self.inserted,
            "duplicate": self.duplicate,
            "invalid": self.invalid,
        }


def _default_validator(payload: dict[str, Any]) -> None:
    # Imported lazily so the module stays import-safe when validation is off.
    from services.ingest.common.document_contracts import validate_document

    validate_document(payload)


class DocumentStore:
    """Idempotent, contract-validated, deduped sink for ``Document`` records."""

    def __init__(self, conn, validator: Validator | None = None):
        self.conn = conn
        self._validator = validator or _default_validator
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.execute(_SCHEMA)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_content_hash "
            "ON documents (content_hash, source_type)"
        )
        from src.ingestion.revisions import DocumentRevisionStore

        self.revisions = DocumentRevisionStore(self.conn)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS document_content_blobs (
            blob_hash TEXT PRIMARY KEY, content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS document_revision_content (
            revision_id TEXT PRIMARY KEY, blob_hash TEXT NOT NULL)""")

    def upsert(
        self,
        documents: Iterable[Document | dict[str, Any]],
        validate: bool = True,
    ) -> UpsertSummary:
        """Validate, dedup, and insert ``documents``; return a summary.

        A validation failure dead-letters that one document. Distinct source
        observations retain their identities even when their bytes match. An
        existing identity is compared with its current immutable revision and
        appended only when content, stable metadata, or lifecycle changed.
        """
        from src.ingestion.canonical import canonicalize_url, content_hash

        summary = UpsertSummary()

        for item in documents:
            summary.received += 1

            # Normalize to a payload dict *without* constructing a Document yet:
            # Document.__post_init__ rejects out-of-contract source_types, and a
            # bad payload must dead-letter rather than abort the whole batch.
            payload = item.to_dict() if isinstance(item, Document) else dict(item)
            doc_id = payload.get("document_id", "unknown")

            if validate:
                try:
                    self._validator(payload)
                except Exception as exc:  # noqa: BLE001 - one bad doc never aborts
                    summary.invalid += 1
                    summary.dead_letter.append(
                        {"document_id": doc_id, "error": str(exc)}
                    )
                    logger.warning("document-store: dead-letter %s (%s)", doc_id, exc)
                    continue

            # Safe now: a validated payload has a contract-valid source_type. With
            # validation off, a malformed payload still dead-letters here.
            try:
                doc = (
                    item if isinstance(item, Document) else Document.from_dict(payload)
                )
            except Exception as exc:  # noqa: BLE001
                summary.invalid += 1
                summary.dead_letter.append({"document_id": doc_id, "error": str(exc)})
                logger.warning("document-store: dead-letter %s (%s)", doc_id, exc)
                continue

            chash = content_hash(doc.content or "")
            current = self.get(doc.document_id)
            payload = doc.to_dict()
            change = self.revisions.observe(payload)
            if doc.content is not None:
                # Exact-byte storage identity is distinct from the normalized
                # content similarity hash. Compatibility rows retain their text
                # while revisions share this canonical content object.
                blob_hash = hashlib.sha256(doc.content.encode("utf-8")).hexdigest()
                self.conn.execute("INSERT OR IGNORE INTO document_content_blobs VALUES (?,?)", [blob_hash, doc.content])
                self.conn.execute("INSERT OR IGNORE INTO document_revision_content VALUES (?,?)", [change["revision_id"], blob_hash])
            summary.changes.append(change)
            if not change["appended"]:
                summary.duplicate += 1
                continue
            row = self._to_row(
                doc, canonicalize_url(doc.url) if doc.url else None, chash
            )
            if current is None:
                self.conn.execute(
                    """
                    INSERT INTO documents
                        (document_id, source_type, language, ingested_at, created_at,
                         source_id, url, canonical_url, content_hash, title, content,
                         content_ref, authors, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    row,
                )
                summary.inserted += 1
            else:
                self.conn.execute(
                    """
                    UPDATE documents SET source_type=?,language=?,ingested_at=?,created_at=?,
                      source_id=?,url=?,canonical_url=?,content_hash=?,title=?,content=?,
                      content_ref=?,authors=?,metadata=? WHERE document_id=?
                    """,
                    [*row[1:], row[0]],
                )
                summary.updated += 1
                if change["change_kind"] == "retracted":
                    summary.retracted += 1
                elif change["change_kind"] == "deleted":
                    summary.deleted += 1
            from src.kb.temporal import store_document_times

            store_document_times(self.conn, payload)
        return summary

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def get(self, document_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM documents WHERE document_id = ?",
            [document_id],
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def list_documents(
        self,
        source_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return stored documents, most-recently-ingested first.

        Optionally filtered by ``source_type`` and paged by ``limit``/``offset``.
        """
        query = f"SELECT {', '.join(_COLUMNS)} FROM documents"
        params: list[Any] = []
        if source_type:
            query += " WHERE source_type = ?"
            params.append(source_type)
        query += " ORDER BY ingested_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete(self, document_id: str) -> bool:
        """Tombstone then remove a document from the compatibility projection."""
        current = self.get(document_id)
        existed = current is not None
        if current is not None:
            current["ingested_at"] = current.get("ingested_at") or 0
            current["metadata"] = {
                **dict(current.get("metadata") or {}),
                "tombstone": True,
                "lifecycle": "deleted",
            }
            self.revisions.observe(current, committed_watermark=0, stage_change=False)
            self.conn.execute(
                "DELETE FROM documents WHERE document_id = ?", [document_id]
            )
        return existed

    # ------------------------------------------------------------------ #

    @staticmethod
    def _row_to_dict(row) -> dict[str, Any]:
        rec = dict(zip(_COLUMNS, row))
        rec["authors"] = json.loads(rec["authors"]) if rec["authors"] else []
        rec["metadata"] = json.loads(rec["metadata"]) if rec["metadata"] else {}
        return rec

    @staticmethod
    def _to_row(doc: Document, canonical_url: str | None, chash: str) -> tuple:
        return (
            doc.document_id,
            doc.source_type,
            doc.language,
            doc.ingested_at,
            doc.created_at,
            doc.source_id,
            doc.url,
            canonical_url,
            chash,
            doc.title,
            doc.content,
            doc.content_ref,
            json.dumps(doc.authors or []),
            json.dumps(doc.metadata or {}),
        )
