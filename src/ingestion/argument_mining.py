"""Incremental argument-mining pass over the unified document corpus.

The scan ledger keys work by ``document_id`` and ``content_hash``. New
documents are mined once, corrected documents are mined again, and failed rows
remain visible for retry/operations. This stage is called by the scheduled
pipeline before stance/drift jobs so those jobs consume fresh claims.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

from services.ingest.common.document_model import Document

_DDL = """
CREATE TABLE IF NOT EXISTS argument_mining_scans (
    document_id     VARCHAR PRIMARY KEY,
    content_hash    VARCHAR,
    processed_at    TIMESTAMP,
    status          VARCHAR NOT NULL,
    prediction_mode VARCHAR,
    claims_count    INTEGER DEFAULT 0,
    evidence_count  INTEGER DEFAULT 0,
    error            VARCHAR
)
"""


def _enabled() -> bool:
    return os.getenv("NOESIS_ARGUMENT_MINING_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off", "disabled",
    }


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def ensure_scan_schema(conn) -> None:
    conn.execute(_DDL)


def mining_configuration(configuration=None):
    """Fingerprint model provenance, extraction rules, and caller configuration."""
    from src.argument_mining import models, frames, evidence, dataset, model_registry
    modules = [models, frames, evidence, dataset]
    modes = {}
    for name, getter in (("claim", models.get_claim_detector), ("frame", frames.get_frame_classifier)):
        try:
            modes[name] = getter().prediction_mode
        except Exception as exc:
            modes[name] = "unavailable:" + type(exc).__name__
    checkpoints = {}
    for directory in (models._CLAIM_MODEL_DIR, frames._FRAME_MODEL_DIR):
        if directory.exists():
            checkpoints[str(directory)] = [(str(p.relative_to(directory)), p.stat().st_size, p.stat().st_mtime_ns)
                                            for p in sorted(directory.rglob("*")) if p.is_file()]
    return {"schema": "argument-mining-v2", "models": model_registry.resolved_pins(),
            "lock": model_registry.read_lock(), "modes": modes, "checkpoints": checkpoints,
            "rules": {m.__name__: hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in modules},
            "evidence_corpus_limit": os.getenv("NOESIS_EVIDENCE_CORPUS_LIMIT", "5000"),
            "configuration": dict(configuration or {})}


def _document(values: tuple[Any, ...]) -> Document:
    (document_id, source_type, language, ingested_at, created_at, source_id,
     url, title, content, content_ref, authors, metadata) = values
    return Document(
        document_id=document_id,
        source_type=source_type,
        language=language or "en",
        ingested_at=int(ingested_at or 0),
        created_at=int(created_at) if created_at is not None else None,
        source_id=source_id,
        url=url,
        title=title,
        content=content,
        content_ref=content_ref,
        authors=json.loads(authors) if isinstance(authors, str) and authors else (authors or []),
        metadata=json.loads(metadata) if isinstance(metadata, str) and metadata else (metadata or {}),
    )


def _record_legislative_metadata(conn, document: Document) -> int:
    metadata = document.metadata or {}
    if metadata.get("record_type") not in {"legislative_vote", "court_ruling"}:
        return 0
    from src.ingestion.connectors.legislative import VoteRecord, record_vote

    record_vote(conn, VoteRecord(
        actor=str(metadata.get("actor") or "").strip(),
        topic=str(metadata.get("topic") or metadata.get("bill") or "").strip(),
        bill=metadata.get("bill"),
        position=str(metadata.get("position") or "").strip(),
        date=metadata.get("date") or document.created_at,
        source=metadata.get("source") or document.url,
        document_id=document.document_id,
    ))
    return 1


def mine_unprocessed_documents(conn, limit: Optional[int] = None, *, graph_publisher=None, configuration=None) -> dict[str, Any]:
    """Mine new/revised documents and return an operational summary.

    Set ``NOESIS_ARGUMENT_MINING_ENABLED=false`` for an explicit opt-out.
    ``NOESIS_ARGUMENT_MINING_BATCH_SIZE`` bounds each scheduled pass and
    ``NOESIS_ARGUMENT_MINING_MODEL_BUDGET`` is a hard per-pass document cap.
    """
    from src.database.local_warehouse_seed import ensure_schema
    from src.ingestion.document_store import DocumentStore
    from src.ingestion import graph_projection
    from src.ingestion.processing_versions import ProcessingVersions, configuration_hash, document_input_hash

    DocumentStore(conn)
    ensure_schema(conn)
    ensure_scan_schema(conn)
    graph_projection.ensure_schema(conn)
    if not _enabled():
        return {"status": "disabled", "processed": 0, "failed": 0, **mining_freshness(conn)}

    versions = ProcessingVersions(conn)
    config_hash = configuration_hash(mining_configuration(configuration))

    batch = limit if limit is not None else _positive_int("NOESIS_ARGUMENT_MINING_BATCH_SIZE", 100)
    budget = _positive_int("NOESIS_ARGUMENT_MINING_MODEL_BUDGET", batch)
    take = min(max(0, int(batch)), budget)
    rows = conn.execute(
        f"""
        SELECT d.document_id, d.source_type, d.language, d.ingested_at,
               d.created_at, d.source_id, d.url, d.title, d.content,
               d.content_ref, d.authors, d.metadata, d.content_hash, {document_input_hash()}
        FROM documents d
        LEFT JOIN argument_mining_scans s ON s.document_id = d.document_id
        LEFT JOIN document_processing_versions v ON v.document_id=d.document_id AND v.stage='argument_mining'
        WHERE d.content IS NOT NULL AND length(trim(d.content)) > 0
          AND (s.document_id IS NULL OR s.content_hash IS DISTINCT FROM d.content_hash
               OR s.status = 'failed' OR v.input_hash IS DISTINCT FROM {document_input_hash()}
               OR v.configuration_hash IS DISTINCT FROM ?)
        ORDER BY d.ingested_at ASC
        LIMIT ?
        """,
        [config_hash, take],
    ).fetchall()

    from src.argument_mining.evidence import run_pipeline
    from src.argument_mining.frames import classify_and_store
    from src.argument_mining.models import get_claim_detector

    processed = failed = claims_total = evidence_total = votes_total = 0
    errors: list[dict[str, str]] = []
    for row in rows:
        doc = _document(row[:-2])
        content_hash, input_hash = row[-2:]
        now = datetime.now(timezone.utc)
        conn.execute("BEGIN TRANSACTION")
        try:
            # A revision replaces derived rows instead of mixing old/new claims.
            conn.execute(
                "DELETE FROM claim_evidence WHERE claim_id IN "
                "(SELECT claim_id FROM argument_claims WHERE document_id = ?)",
                [doc.document_id],
            )
            conn.execute("DELETE FROM argument_claims WHERE document_id = ?", [doc.document_id])
            claims, evidence = run_pipeline(doc, conn)
            classify_and_store(doc, conn)
            document_votes = _record_legislative_metadata(conn, doc)
            mode = get_claim_detector().prediction_mode
            conn.execute(
                """INSERT INTO argument_mining_scans
                   (document_id, content_hash, processed_at, status,
                    prediction_mode, claims_count, evidence_count, error)
                   VALUES (?, ?, ?, 'complete', ?, ?, ?, NULL)
                   ON CONFLICT (document_id) DO UPDATE SET
                     content_hash=excluded.content_hash,
                     processed_at=excluded.processed_at, status='complete',
                     prediction_mode=excluded.prediction_mode,
                     claims_count=excluded.claims_count,
                     evidence_count=excluded.evidence_count, error=NULL""",
                [doc.document_id, content_hash, now, mode, len(claims), len(evidence)],
            )
            versions.record(doc.document_id, "argument_mining", input_hash, config_hash)
            graph_projection.enqueue(conn, doc, input_hash)
            conn.execute("COMMIT")
            processed += 1
            votes_total += document_votes
            claims_total += len(claims)
            evidence_total += len(evidence)
        except Exception as exc:  # one document must not abort the batch
            conn.execute("ROLLBACK")
            message = str(exc)[:500]
            conn.execute(
                """INSERT INTO argument_mining_scans
                   (document_id, content_hash, processed_at, status, error)
                   VALUES (?, ?, ?, 'failed', ?)
                   ON CONFLICT (document_id) DO UPDATE SET
                     content_hash=excluded.content_hash,
                     processed_at=excluded.processed_at, status='failed', error=excluded.error""",
                [doc.document_id, content_hash, now, message],
            )
            failed += 1
            errors.append({"document_id": doc.document_id, "error": message})
            continue

    # Migrate successful legacy scans without rerunning their inference.
    legacy = conn.execute("""SELECT d.document_id,d.source_type,d.language,d.ingested_at,
        d.created_at,d.source_id,d.url,d.title,d.content,d.content_ref,d.authors,d.metadata,d.content_hash
        FROM documents d JOIN argument_mining_scans s ON s.document_id=d.document_id
        LEFT JOIN argument_graph_projections g ON g.document_id=d.document_id
        WHERE s.status='complete' AND s.content_hash IS NOT DISTINCT FROM d.content_hash
        AND g.document_id IS NULL ORDER BY d.document_id LIMIT ?""", [take]).fetchall()
    for row in legacy:
        graph_projection.enqueue(conn, _document(row[:-1]), row[-1] or "")
    graph_summary = graph_projection.retry_graph_projections(conn, limit=take, publisher=graph_publisher)

    relation_summary: dict[str, Any] = {}
    try:
        from src.argument_mining.relations import extract_document_relations
        relation_summary = extract_document_relations(conn, limit=take)
    except Exception as exc:
        relation_summary = {"error": str(exc)}

    return {
        "status": "partial" if failed else "complete", "processed": processed, "failed": failed,
        "claims": claims_total, "evidence": evidence_total,
        "legislative_records": votes_total, "budget": budget,
        "document_relations": relation_summary,
        "graph_projection": graph_summary,
        "errors": errors[:10], **mining_freshness(conn, config_hash=config_hash),
    }


def mining_freshness(conn, *, config_hash=None) -> dict[str, Any]:
    """Corpus/ledger freshness numbers used by API and MCP status surfaces."""
    ensure_scan_schema(conn)
    from src.ingestion.processing_versions import ProcessingVersions, configuration_hash, document_input_hash
    ProcessingVersions(conn)
    config_hash = config_hash or configuration_hash(mining_configuration())
    try:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM documents WHERE content IS NOT NULL AND length(trim(content)) > 0"
        ).fetchone()[0])
        fresh = int(conn.execute(
            f"""SELECT COUNT(*) FROM documents d JOIN argument_mining_scans s
               ON s.document_id=d.document_id
               JOIN document_processing_versions v ON v.document_id=d.document_id AND v.stage='argument_mining'
               WHERE s.status='complete'
                 AND s.content_hash IS NOT DISTINCT FROM d.content_hash
                 AND v.input_hash={document_input_hash()} AND v.configuration_hash=?""", [config_hash]
        ).fetchone()[0])
        last = conn.execute(
            "SELECT MAX(processed_at) FROM argument_mining_scans WHERE status='complete'"
        ).fetchone()[0]
    except Exception:
        total, fresh, last = 0, 0, None
    from src.ingestion.graph_projection import projection_freshness
    return {
        **projection_freshness(conn),
        "documents_ingested": total,
        "documents_mined": fresh,
        "documents_pending": max(0, total - fresh),
        "freshness_ratio": round(fresh / total, 4) if total else 1.0,
        "last_mined_at": last.isoformat() if hasattr(last, "isoformat") else last,
    }
