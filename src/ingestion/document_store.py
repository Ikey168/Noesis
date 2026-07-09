"""
Unified, validated, deduped document sink (#894, #893, #897).

Persistence used to be fragmented — ``news_articles`` (exact-URL dedup),
``dataset_observations`` (vintage upsert), ``assets`` (sha256), per-connector
``ingest_to_kg`` — with **no** ``documents`` table and no single place that
validates against the contract, dedups, or makes ingestion idempotent.

``DocumentStore`` is that place. ``upsert(documents)``:

- validates each document against ``document-ingest-v1`` (#893) — invalid ones
  are dead-lettered (returned + logged), never written;
- canonicalizes the URL and computes a content hash (#895) — skipping exact
  re-inserts (``document_id``) and content duplicates (``content_hash`` +
  ``source_type``, so the same story syndicated under two URLs collapses);
- inserts the survivors idempotently;

and returns an :class:`UpsertSummary` (#897). The DuckDB connection is injected,
so the store is offline-testable against an in-memory database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from services.ingest.common.document_model import Document

logger = logging.getLogger(__name__)

# A validator raises on an invalid payload; the default uses document-ingest-v1.
Validator = Callable[[Dict[str, Any]], None]

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
    duplicate: int = 0
    invalid: int = 0
    dead_letter: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "received": self.received,
            "inserted": self.inserted,
            "duplicate": self.duplicate,
            "invalid": self.invalid,
        }


def _default_validator(payload: Dict[str, Any]) -> None:
    # Imported lazily so the module stays import-safe when validation is off.
    from services.ingest.common.document_contracts import validate_document

    validate_document(payload)


class DocumentStore:
    """Idempotent, contract-validated, deduped sink for ``Document`` records."""

    def __init__(self, conn, validator: Optional[Validator] = None):
        self.conn = conn
        self._validator = validator or _default_validator
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.execute(_SCHEMA)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_content_hash "
            "ON documents (content_hash, source_type)"
        )

    def upsert(
        self,
        documents: Iterable[Union[Document, Dict[str, Any]]],
        validate: bool = True,
    ) -> UpsertSummary:
        """Validate, dedup, and insert ``documents``; return a summary.

        A validation failure dead-letters that one document (the rest of the
        batch still stores). Duplicates — same ``document_id``, or same
        ``(content_hash, source_type)`` — are skipped. Re-running an
        already-ingested batch inserts nothing.
        """
        from src.ingestion.canonical import canonicalize_url, content_hash

        summary = UpsertSummary()
        seen_ids, seen_hashes = self._load_existing()
        rows: List[Tuple] = []

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
                    summary.dead_letter.append({"document_id": doc_id, "error": str(exc)})
                    logger.warning("document-store: dead-letter %s (%s)", doc_id, exc)
                    continue

            # Safe now: a validated payload has a contract-valid source_type. With
            # validation off, a malformed payload still dead-letters here.
            try:
                doc = item if isinstance(item, Document) else Document.from_dict(payload)
            except Exception as exc:  # noqa: BLE001
                summary.invalid += 1
                summary.dead_letter.append({"document_id": doc_id, "error": str(exc)})
                logger.warning("document-store: dead-letter %s (%s)", doc_id, exc)
                continue

            chash = content_hash(doc.content or "")
            hkey = (chash, doc.source_type)
            if doc.document_id in seen_ids or hkey in seen_hashes:
                summary.duplicate += 1
                continue

            seen_ids.add(doc.document_id)
            seen_hashes.add(hkey)
            rows.append(self._to_row(doc, canonicalize_url(doc.url) if doc.url else None, chash))

        if rows:
            self.conn.executemany(
                """
                INSERT INTO documents
                    (document_id, source_type, language, ingested_at, created_at,
                     source_id, url, canonical_url, content_hash, title, content,
                     content_ref, authors, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            summary.inserted = len(rows)
        return summary

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def get(self, document_id: str) -> Optional[Dict[str, Any]]:
        cols = [
            "document_id", "source_type", "language", "ingested_at", "created_at",
            "source_id", "url", "canonical_url", "content_hash", "title",
            "content", "content_ref", "authors", "metadata",
        ]
        row = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM documents WHERE document_id = ?",
            [document_id],
        ).fetchone()
        if row is None:
            return None
        rec = dict(zip(cols, row))
        rec["authors"] = json.loads(rec["authors"]) if rec["authors"] else []
        rec["metadata"] = json.loads(rec["metadata"]) if rec["metadata"] else {}
        return rec

    # ------------------------------------------------------------------ #

    def _load_existing(self):
        ids = {
            r[0] for r in self.conn.execute("SELECT document_id FROM documents").fetchall()
        }
        hashes = {
            (r[0], r[1])
            for r in self.conn.execute(
                "SELECT content_hash, source_type FROM documents "
                "WHERE content_hash IS NOT NULL"
            ).fetchall()
        }
        return ids, hashes

    @staticmethod
    def _to_row(doc: Document, canonical_url: Optional[str], chash: str) -> Tuple:
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
