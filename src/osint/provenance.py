"""
``trace_artifact(claim_id | document_id)`` - the provenance chain (issue #639,
completing the #614 "provenance chain is inspectable" bullet).

The OSINT evidence discipline puts a citation on every rendered line; this
takes the next step and traces an artifact end to end, from the source that
carried it, through the document, its enrichments (claims, entities, frames),
to the claim, and on to any provisioned KG namespace it was routed into. It is
pure composition of layers Noesis already builds, so nothing new is ingested;
the trace just makes the existing chain legible in one read.

The chain is ordered oldest-first (source first, claim last) and every stage
carries its own citation, so an analyst can answer "where did this come from,
and what happened to it" without leaving the panel. An artifact whose document
does not resolve to the corpus is flagged ``cited: false`` rather than hidden.

Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.osint import common, evidence


def _document_row(conn, document_id: str) -> Optional[Dict[str, Any]]:
    if not common.table_exists(conn, "news_articles"):
        return None
    row = conn.execute(
        "SELECT id, title, source, url, publish_date, category "
        "FROM news_articles WHERE id = ?",
        [document_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "document_id": row[0],
        "title": row[1],
        "source": row[2],
        "url": row[3],
        "published_at": str(row[4]) if row[4] is not None else None,
        "category": row[5],
    }


def _enrichment_summary(conn, document_id: str) -> Dict[str, Any]:
    """What the enrichers produced from this document: its claims, extracted
    entities, and framing. Each count links back to the document."""
    out: Dict[str, Any] = {"claims": [], "entities": [], "frames": []}
    if common.table_exists(conn, "argument_claims"):
        rows = conn.execute(
            "SELECT claim_id, claim_text, COALESCE(factcheck_verdict, 'unverified') "
            "FROM argument_claims WHERE document_id = ?",
            [document_id],
        ).fetchall()
        out["claims"] = [
            {"claim_id": r[0], "text": (r[1] or "")[:180], "verdict": r[2]}
            for r in rows
        ]
    if common.table_exists(conn, "document_actors"):
        rows = conn.execute(
            "SELECT DISTINCT actor_name, role FROM document_actors "
            "WHERE document_id = ? LIMIT 25",
            [document_id],
        ).fetchall()
        out["entities"] = [{"entity": r[0], "role": r[1]} for r in rows]
    if common.table_exists(conn, "document_frames"):
        try:
            rows = conn.execute(
                "SELECT frame, score FROM document_frames WHERE document_id = ? "
                "ORDER BY score DESC NULLS LAST LIMIT 10",
                [document_id],
            ).fetchall()
            out["frames"] = [
                {"frame": r[0], "score": float(r[1]) if r[1] is not None else None}
                for r in rows
            ]
        except Exception:
            out["frames"] = []
    return out


def _routed_namespaces(conn, document_id: str) -> List[Dict[str, Any]]:
    """Provisioned KG namespaces this document was routed into, with the
    provisioning that put it there (the Track P audit trail)."""
    if not common.table_exists(conn, "provisioned_kgs"):
        return []
    out = []
    try:
        kgs = conn.execute(
            "SELECT name FROM provisioned_kgs WHERE status = 'deployed'"
        ).fetchall()
    except Exception:
        return []
    for (name,) in kgs:
        table = f"kg_{name}_documents"
        if not common.table_exists(conn, table):
            continue
        try:
            hit = conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1", [document_id]
            ).fetchone()
        except Exception:
            hit = None
        if hit:
            out.append({"kg": name, "namespace": f"kg_{name}_"})
    return out


def trace_artifact(
    conn,
    claim_id: Optional[str] = None,
    document_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Trace one artifact (a claim or a document) from source to panel.

    Returns an ordered ``chain`` of stages, each cited: the source/connector
    that ingested it, the document, the enrichments derived from it, the claim
    (when tracing a claim), and any provisioned KG namespace it was routed
    into. ``cited`` is False when the document does not resolve to the corpus.
    """
    claim: Optional[Dict[str, Any]] = None
    if claim_id is not None:
        if not common.table_exists(conn, "argument_claims"):
            return {"error": "no claim layer available"}
        row = conn.execute(
            "SELECT claim_id, claim_text, document_id, source_type, "
            "COALESCE(factcheck_verdict, 'unverified') "
            "FROM argument_claims WHERE claim_id = ?",
            [claim_id],
        ).fetchone()
        if row is None:
            return {"error": f"claim {claim_id!r} not found", "code": "not_found"}
        claim = {
            "claim_id": row[0],
            "text": (row[1] or "")[:280],
            "verdict": row[4],
            "source_type": row[3],
        }
        document_id = row[2]
    elif document_id is None:
        return {"error": "provide claim_id or document_id", "code": "no_input"}

    document = _document_row(conn, document_id)
    cited = document is not None

    chain: List[Dict[str, Any]] = []
    # Stage 1: source / connector (the ingestion origin).
    source = document["source"] if document else (claim or {}).get("source_type") or "unknown"
    chain.append({
        "stage": "source",
        "source": source,
        "source_type": (claim or {}).get("source_type"),
        "cite": evidence.citation(document_id, source, document["url"] if document else None,
                                  resolved=cited),
    })
    # Stage 2: document.
    chain.append({
        "stage": "document",
        "document_id": document_id,
        "title": document["title"] if document else None,
        "url": document["url"] if document else None,
        "published_at": document["published_at"] if document else None,
        "cite": evidence.citation(document_id, source, document["url"] if document else None,
                                  resolved=cited),
    })
    # Stage 3: enrichment (what was derived from the document).
    enrich = _enrichment_summary(conn, document_id) if document_id else {"claims": [], "entities": [], "frames": []}
    chain.append({
        "stage": "enrichment",
        "claim_count": len(enrich["claims"]),
        "entity_count": len(enrich["entities"]),
        "frame_count": len(enrich["frames"]),
        "entities": enrich["entities"][:10],
        "frames": enrich["frames"],
    })
    # Stage 4: the claim (only when tracing a claim).
    if claim is not None:
        chain.append({"stage": "claim", **claim})
    # Stage 5: routed namespaces (Track P provenance), when present.
    namespaces = _routed_namespaces(conn, document_id) if document_id else []
    if namespaces:
        chain.append({"stage": "namespaces", "routed_into": namespaces})

    return {
        "artifact": {"type": "claim" if claim_id else "document", "id": claim_id or document_id},
        "cited": cited,
        "chain": chain,
        "stage_count": len(chain),
        "claims": enrich["claims"],
    }
