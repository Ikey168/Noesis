"""Durable, independently retryable graph projection of mined documents."""

import json
from datetime import datetime, timezone


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS argument_graph_projections (
        document_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
        status TEXT NOT NULL, attempts BIGINT NOT NULL DEFAULT 0,
        queued_at TIMESTAMP NOT NULL, completed_at TIMESTAMP, last_error TEXT)""")


def enqueue(conn, document, input_hash):
    """Called inside the transaction publishing successful inference."""
    conn.execute("""INSERT INTO argument_graph_projections
        (document_id,input_hash,payload_json,status,queued_at)
        VALUES (?,?,?,'pending',?) ON CONFLICT(document_id) DO UPDATE SET
        input_hash=excluded.input_hash,payload_json=excluded.payload_json,
        status='pending',attempts=0,queued_at=excluded.queued_at,
        completed_at=NULL,last_error=NULL""",
        [document.document_id, input_hash, json.dumps(document.to_dict()), datetime.now(timezone.utc)])


def retry_graph_projections(conn, *, limit=100, publisher=None):
    ensure_schema(conn)
    rows = conn.execute("""SELECT document_id,input_hash,payload_json FROM argument_graph_projections
        WHERE status <> 'complete' ORDER BY queued_at,document_id LIMIT ?""",
        [min(max(int(limit), 0), 1000)]).fetchall()
    if publisher is None and rows:
        from src.knowledge_graph.foundation import DuckDBKnowledgeGraphStore
        from src.knowledge_graph.kg_updater import update_from_document

        def publisher(document):
            # Reopen the object projection each attempt: a previous partial failure
            # must not leave an optimistic in-memory cache marking writes successful.
            store = DuckDBKnowledgeGraphStore(connection=conn)
            update_from_document(document, store=store, strict=True)

    completed = failed = 0
    for document_id, input_hash, payload in rows:
        try:
            publisher(json.loads(payload))
        except Exception as exc:
            conn.execute("""UPDATE argument_graph_projections SET status='failed',
                attempts=attempts+1,last_error=? WHERE document_id=? AND input_hash=?""",
                [str(exc)[:500], document_id, input_hash])
            failed += 1
        else:
            conn.execute("""UPDATE argument_graph_projections SET status='complete',
                attempts=attempts+1,last_error=NULL,completed_at=?
                WHERE document_id=? AND input_hash=?""",
                [datetime.now(timezone.utc), document_id, input_hash])
            completed += 1
    return {"completed": completed, "failed": failed, **projection_freshness(conn)}


def projection_freshness(conn):
    ensure_schema(conn)
    pending, oldest = conn.execute("""SELECT count(*),min(queued_at)
        FROM argument_graph_projections WHERE status <> 'complete'""").fetchone()
    return {"graph_documents_pending": pending,
            "graph_oldest_pending_at": oldest.isoformat() if oldest else None}
