"""
Argument Mining API routes (Issues #90, #93, #95, #96, #97, #99, #102, #105, #110, #111, #112).

GET  /api/v1/arguments/frames                    — aggregate frame distribution
GET  /api/v1/arguments/frames?document_id=       — frames for a specific document
GET  /api/v1/arguments/frames?source_type=       — aggregate filtered by source type
GET  /api/v1/arguments/frames?date_range=        — aggregate filtered by time window (7d/30d/90d)
GET  /api/v1/arguments/frames/source             — per-source frame distribution with concentrated-framing flag (#105)
GET  /api/v1/arguments/claims                    — query stored claims (includes factcheck fields)
GET  /api/v1/arguments/claims?unsourced_only=    — claims with no corroborating evidence
POST /api/v1/arguments/claims/extract            — run pipeline on a stored document
GET  /api/v1/arguments/claims/{id}               — single claim with evidence links + factcheck verdict
GET  /api/v1/arguments/claims/{id}/evidence      — evidence for a claim
POST /api/v1/arguments/claims/{id}/factcheck     — trigger on-demand fact-check (#97)
GET  /api/v1/arguments/stance/sources            — stance per (source, topic) from source_stances table (#99)
GET  /api/v1/arguments/stance/drift              — structured drift events + 7-bucket time series (#102)
GET  /api/v1/arguments/stance                    — stance distribution across topics
GET  /api/v1/arguments/positions                 — actor policy positions with follow-through history (#110, #111)
POST /api/v1/arguments/positions/extract         — run position extraction on a stored document (#110)
POST /api/v1/arguments/positions/{id}/check      — trigger follow-through check for one position (#111)
GET  /api/v1/arguments/controversy               — conflict pairs ranked by similarity (#112 uses claim_conflicts, fallback claim_evidence)
GET  /api/v1/arguments/controversy/graph         — force-directed graph: nodes=claims, edges=conflicts (#96, #112)
POST /api/v1/arguments/controversy/compute       — trigger on-demand conflict detection batch (#112)
GET  /api/v1/arguments/sources/ranking           — sources ranked by claim quality
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/v1/arguments", tags=["arguments"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_date_cutoff(date_range: Optional[str]) -> Optional[str]:
    """Parse '7d', '30d', '90d' → ISO date cutoff string, or None."""
    if not date_range:
        return None
    dr = date_range.strip().lower()
    if dr.endswith("d"):
        try:
            from datetime import datetime, timezone, timedelta
            days = int(dr[:-1])
            return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        except ValueError:
            pass
    return None


def _classify_stance(supports: int, contradicts: int, confidence: float) -> str:
    """Map evidence counts + confidence to a 4-class stance label."""
    if confidence < 0.4:
        return "ambiguous"
    if contradicts > supports and contradicts > 0:
        return "critical"
    if supports > 0:
        return "supportive"
    return "neutral"


# ---------------------------------------------------------------------------
# Frames endpoint
# ---------------------------------------------------------------------------

@router.get("/frames")
async def get_frames(
    document_id: Optional[str] = Query(None, description="Return frames for this document"),
    source_type: Optional[str] = Query(None, description="Filter aggregate by source type"),
    date_range: Optional[str] = Query(None, description="Time window: 7d, 30d, 90d"),
    limit: int = Query(50, ge=1, le=500, description="Max rows for aggregate queries"),
) -> Dict[str, Any]:
    """
    Return narrative frame scores.

    - With `document_id`: look up pre-computed scores in `document_frames`; if
      absent, classify the document on-the-fly from `news_articles` content.
    - Without `document_id`: return average scores across all documents,
      optionally filtered by `source_type`.
    """
    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()

    if document_id:
        return await _frames_for_document(conn, document_id)
    return await _aggregate_frames(conn, source_type=source_type, limit=limit, date_range=date_range)


async def _frames_for_document(conn, document_id: str) -> Dict[str, Any]:
    """Return per-frame scores for one document, classifying on-the-fly if needed."""
    import threading

    lock = getattr(conn, "_lock", None) or threading.Lock()

    # Check pre-computed scores first
    with lock:
        rows = conn.execute(
            "SELECT frame, score, source_type FROM document_frames WHERE document_id = ?",
            [document_id],
        ).fetchall()

    if rows:
        frames = {r[0]: round(r[1], 4) for r in rows}
        source_type = rows[0][2]
        dominant = max(frames, key=frames.__getitem__)
        return {
            "document_id": document_id,
            "source_type": source_type,
            "frames": frames,
            "dominant": dominant,
            "source": "precomputed",
        }

    # Not in cache — try to classify on-the-fly from news_articles
    with lock:
        article = conn.execute(
            "SELECT id, title, content, source FROM news_articles WHERE id = ? LIMIT 1",
            [document_id],
        ).fetchone()

    if not article:
        raise HTTPException(
            status_code=404,
            detail=f"Document '{document_id}' not found in document_frames or news_articles",
        )

    doc_id, title, content = article[0], article[1], article[2]

    try:
        from src.argument_mining.frames import get_frame_classifier
        fc = get_frame_classifier()
        from services.ingest.common.document_model import Document
        import time
        doc = Document(
            document_id=doc_id,
            source_type="news",
            language="en",
            ingested_at=int(time.time() * 1000),
            title=title,
            content=content,
        )
        prediction = fc.predict(doc)
        return {
            "document_id": document_id,
            "source_type": prediction.source_type,
            "frames": {f: round(s, 4) for f, s in prediction.frames.items()},
            "dominant": prediction.dominant,
            "source": "live",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Frame classification failed: {exc}")


async def _aggregate_frames(
    conn,
    source_type: Optional[str],
    limit: int,
    date_range: Optional[str] = None,
) -> Dict[str, Any]:
    """Return average frame scores across all (optionally filtered) documents."""
    import threading

    lock = getattr(conn, "_lock", None) or threading.Lock()
    cutoff = _parse_date_cutoff(date_range)

    conditions: list[str] = []
    params: list[Any] = []

    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)
    if cutoff:
        conditions.append("classified_at >= ?")
        params.append(cutoff)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    with lock:
        rows = conn.execute(
            f"""
            SELECT frame, AVG(score) AS avg_score, COUNT(DISTINCT document_id) AS n_docs
            FROM document_frames
            {where}
            GROUP BY frame
            ORDER BY avg_score DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    if not rows:
        # No pre-computed data — classify a sample and return live estimates
        return await _aggregate_live(conn, source_type=source_type)

    total_docs = rows[0][2] if rows else 0
    distribution = {r[0]: round(r[1], 4) for r in rows}
    dominant = max(distribution, key=distribution.__getitem__) if distribution else "other"
    return {
        "distribution": distribution,
        "dominant": dominant,
        "total_documents": total_docs,
        "source_type_filter": source_type,
        "source": "precomputed",
    }


async def _aggregate_live(conn, source_type: Optional[str]) -> Dict[str, Any]:
    """Classify a sample of news_articles and return aggregate frame distribution."""
    import threading
    import time

    lock = getattr(conn, "_lock", None) or threading.Lock()

    with lock:
        rows = conn.execute(
            "SELECT id, title, content FROM news_articles LIMIT 20"
        ).fetchall()

    if not rows:
        from src.argument_mining.dataset import FRAME_LABELS
        return {
            "distribution": {f: 0.0 for f in FRAME_LABELS},
            "dominant": "other",
            "total_documents": 0,
            "source_type_filter": source_type,
            "source": "live",
        }

    try:
        from src.argument_mining.frames import get_frame_classifier, FRAME_LABELS
        from services.ingest.common.document_model import Document

        fc = get_frame_classifier()
        totals: Dict[str, float] = {f: 0.0 for f in FRAME_LABELS}
        for doc_id, title, content in rows:
            doc = Document(
                document_id=doc_id,
                source_type="news",
                language="en",
                ingested_at=int(time.time() * 1000),
                title=title,
                content=content,
            )
            pred = fc.predict(doc)
            for frame, score in pred.frames.items():
                totals[frame] = totals.get(frame, 0.0) + score

        n = len(rows)
        distribution = {f: round(totals[f] / n, 4) for f in FRAME_LABELS}
        dominant = max(distribution, key=distribution.__getitem__)
        return {
            "distribution": distribution,
            "dominant": dominant,
            "total_documents": n,
            "source_type_filter": source_type,
            "source": "live",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Live frame classification failed: {exc}")


# ---------------------------------------------------------------------------
# Claims endpoints (#93)
# ---------------------------------------------------------------------------

@router.get("/claims")
async def get_claims(
    document_id: Optional[str] = Query(None, description="Filter by document ID"),
    source_type: Optional[str] = Query(None, description="Filter by source type"),
    topic: Optional[str] = Query(None, description="Full-text filter on claim text"),
    unsourced_only: bool = Query(False, description="Return only unsourced (attributed=false) claims"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
) -> Dict[str, Any]:
    """Query stored argument claims, filterable by document, source type, or keyword topic."""
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    conditions: list[str] = []
    params: list[Any] = []

    if document_id:
        conditions.append("document_id = ?")
        params.append(document_id)
    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)
    if topic:
        conditions.append("claim_text ILIKE ?")
        params.append(f"%{topic}%")
    if unsourced_only:
        # attributed IS NULL means not yet classified — treat as unsourced
        conditions.append("(attributed = false OR attributed IS NULL)")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    with lock:
        rows = conn.execute(
            f"""
            SELECT claim_id, claim_text, document_id, source_type, confidence,
                   extracted_at, factcheck_verdict, factcheck_url, factcheck_publisher,
                   attributed, attribution_text
            FROM argument_claims
            {where}
            ORDER BY confidence DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    claims = [
        {
            "claim_id": r[0],
            "claim_text": r[1],
            "document_id": r[2],
            "source_type": r[3],
            "confidence": round(r[4], 4) if r[4] is not None else None,
            "extracted_at": r[5],
            "factcheck_verdict": r[6],
            "factcheck_url": r[7],
            "factcheck_publisher": r[8],
            "attributed": r[9],
            "attribution_text": r[10],
        }
        for r in rows
    ]
    return {"claims": claims, "count": len(claims)}


@router.post("/claims/classify-attribution")
async def classify_attribution_batch(
    limit: int = Query(500, ge=1, le=5000, description="Max claims to classify per call"),
) -> Dict[str, Any]:
    """
    Run content-type-aware attribution classification over unclassified claims.

    Sweeps ``argument_claims`` rows where ``attributed IS NULL`` and updates
    them with sourced/unsourced verdict and optional attribution snippet.
    Safe to call repeatedly.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection
    from src.argument_mining.attribution import run_attribution_batch

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()
    result = run_attribution_batch(conn, lock, limit=limit)
    return result


@router.post("/claims/extract")
async def extract_claims_for_document(
    document_id: str = Query(..., description="ID of a stored document"),
    source_type: Optional[str] = Query(None, description="Source type for legacy news rows"),
) -> Dict[str, Any]:
    """Run the claim+evidence pipeline on a stored document and persist results."""
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    import duckdb

    with lock:
        try:
            row = conn.execute(
                "SELECT document_id, title, content, source_type "
                "FROM documents WHERE document_id = ? LIMIT 1",
                [document_id],
            ).fetchone()
        except duckdb.CatalogException:
            row = None  # Warehouses created before the generic document store.
        if row is None:
            try:
                legacy = conn.execute(
                    "SELECT id, title, content FROM news_articles WHERE id = ? LIMIT 1",
                    [document_id],
                ).fetchone()
            except duckdb.CatalogException:
                legacy = None
            row = (*legacy, source_type or "news") if legacy else None

    if not row:
        raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found")

    doc_id, title, content, stored_source_type = row
    if not content:
        raise HTTPException(status_code=422, detail="Document has no content to process")

    try:
        import time
        from services.ingest.common.document_model import Document
        from src.argument_mining.evidence import run_pipeline

        doc = Document(
            document_id=doc_id,
            source_type=stored_source_type,
            language="en",
            ingested_at=int(time.time() * 1000),
            title=title,
            content=content,
        )
        claims, evidence = run_pipeline(doc, conn)
        return {
            "document_id": doc_id,
            "source_type": stored_source_type,
            "claims_extracted": len(claims),
            "evidence_found": len(evidence),
            "claims": [
                {
                    "claim_id": c.claim_id,
                    "claim_text": c.claim_text,
                    "confidence": round(c.confidence, 4),
                }
                for c in claims
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pipeline failed: {exc}")


@router.get("/claims/{claim_id}/evidence")
async def get_evidence(
    claim_id: str,
    relation: Optional[str] = Query(None, description="Filter by 'supports' or 'contradicts'"),
    limit: int = Query(20, ge=1, le=200),
) -> Dict[str, Any]:
    """Return stored evidence records for a given claim."""
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    conditions = ["claim_id = ?"]
    params: list[Any] = [claim_id]

    if relation:
        if relation not in ("supports", "contradicts"):
            raise HTTPException(status_code=422, detail="relation must be 'supports' or 'contradicts'")
        conditions.append("relation = ?")
        params.append(relation)

    params.append(limit)
    with lock:
        rows = conn.execute(
            f"""
            SELECT evidence_id, evidence_text, evidence_document_id,
                   evidence_source_type, relation, similarity_score, found_at
            FROM claim_evidence
            WHERE {' AND '.join(conditions)}
            ORDER BY similarity_score DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    # Verify the claim exists (return 404 rather than empty list for unknown IDs)
    if not rows:
        with lock:
            exists = conn.execute(
                "SELECT 1 FROM argument_claims WHERE claim_id = ? LIMIT 1",
                [claim_id],
            ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail=f"Claim '{claim_id}' not found")

    evidence = [
        {
            "evidence_id": r[0],
            "evidence_text": r[1],
            "evidence_document_id": r[2],
            "evidence_source_type": r[3],
            "relation": r[4],
            "similarity_score": round(r[5], 6) if r[5] is not None else None,
            "found_at": r[6],
        }
        for r in rows
    ]
    return {"claim_id": claim_id, "evidence": evidence, "count": len(evidence)}


# ---------------------------------------------------------------------------
# Single claim detail (#95)
# ---------------------------------------------------------------------------

@router.get("/claims/{claim_id}")
async def get_claim(claim_id: str) -> Dict[str, Any]:
    """Return a single claim with its evidence links and factcheck verdict."""
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    with lock:
        row = conn.execute(
            """
            SELECT claim_id, claim_text, document_id, source_type, confidence,
                   extracted_at, factcheck_verdict, factcheck_url, factcheck_publisher
            FROM argument_claims WHERE claim_id = ?
            """,
            [claim_id],
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Claim '{claim_id}' not found")

    with lock:
        ev_rows = conn.execute(
            """
            SELECT evidence_id, evidence_text, evidence_document_id,
                   evidence_source_type, relation, similarity_score, found_at
            FROM claim_evidence WHERE claim_id = ?
            ORDER BY similarity_score DESC
            """,
            [claim_id],
        ).fetchall()

    return {
        "claim_id": row[0],
        "claim_text": row[1],
        "document_id": row[2],
        "source_type": row[3],
        "confidence": round(row[4], 4) if row[4] is not None else None,
        "extracted_at": row[5],
        "factcheck_verdict": row[6],
        "factcheck_url": row[7],
        "factcheck_publisher": row[8],
        "evidence": [
            {
                "evidence_id": e[0],
                "evidence_text": e[1],
                "evidence_document_id": e[2],
                "evidence_source_type": e[3],
                "relation": e[4],
                "similarity_score": round(e[5], 6) if e[5] is not None else None,
                "found_at": e[6],
            }
            for e in ev_rows
        ],
        "evidence_count": len(ev_rows),
    }


# ---------------------------------------------------------------------------
# On-demand fact-check endpoint (#97)
# ---------------------------------------------------------------------------

@router.post("/claims/{claim_id}/factcheck")
async def factcheck_claim(claim_id: str) -> Dict[str, Any]:
    """
    Trigger an on-demand fact-check for a single claim via Google Fact Check Tools.

    Requires GOOGLE_FACTCHECK_API_KEY in the environment.  Returns 503 when the
    key is not configured.  Results are cached in argument_claims so repeated
    calls for the same claim are fast.
    """
    import threading
    import os

    from src.database.local_analytics_connector import get_shared_connection
    from src.argument_mining.factcheck import lookup_claim, store_result

    if not os.getenv("GOOGLE_FACTCHECK_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="GOOGLE_FACTCHECK_API_KEY is not configured on this server.",
        )

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    with lock:
        row = conn.execute(
            "SELECT claim_text FROM argument_claims WHERE claim_id = ?",
            [claim_id],
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Claim '{claim_id}' not found")

    result = lookup_claim(row[0])
    if result is None:
        raise HTTPException(status_code=502, detail="Fact Check Tools API returned no result")

    store_result(conn, lock, claim_id, result)

    return {
        "claim_id": claim_id,
        "factcheck_verdict": result.verdict,
        "factcheck_url": result.url,
        "factcheck_publisher": result.publisher,
        "textual_rating": result.textual_rating,
        "checked_at": result.checked_at,
    }


# ---------------------------------------------------------------------------
# Stance endpoints (#95, #99)
# ---------------------------------------------------------------------------

@router.get("/stance/sources")
async def get_stance_by_source(
    topic: Optional[str] = Query(None, description="Filter by topic/category keyword"),
    source: Optional[str] = Query(None, description="Filter by publication source name"),
    source_type: Optional[str] = Query(None, description="Filter by source type"),
    date_range: Optional[str] = Query(None, description="Time window: 7d, 30d, 90d"),
    limit: int = Query(30, ge=1, le=200),
) -> Dict[str, Any]:
    """
    Return stance breakdown (supportive/critical/neutral/ambiguous) per source and topic,
    aggregated from the `source_stances` table populated by the nightly stance aggregation job.

    Falls back to deriving stances from `argument_claims` when the table is empty.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    cutoff = _parse_date_cutoff(date_range)
    where_parts: list[str] = []
    params: list[Any] = []

    if topic:
        where_parts.append("topic ILIKE ?")
        params.append(f"%{topic}%")
    if source:
        where_parts.append("source ILIKE ?")
        params.append(f"%{source}%")
    if source_type:
        where_parts.append("source_type = ?")
        params.append(source_type)
    if cutoff:
        where_parts.append("window_start >= ?")
        params.append(cutoff)

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)

    with lock:
        rows = conn.execute(
            f"""
            SELECT
                source, source_type, topic,
                SUM(CASE WHEN stance = 'supportive' THEN document_count ELSE 0 END) AS supportive,
                SUM(CASE WHEN stance = 'critical'   THEN document_count ELSE 0 END) AS critical,
                SUM(CASE WHEN stance = 'neutral'    THEN document_count ELSE 0 END) AS neutral,
                SUM(CASE WHEN stance = 'ambiguous'  THEN document_count ELSE 0 END) AS ambiguous,
                SUM(document_count)                                                  AS total,
                AVG(confidence)                                                      AS confidence,
                MAX(window_start)                                                    AS window_start,
                MAX(window_end)                                                      AS window_end
            FROM source_stances
            {where}
            GROUP BY source, source_type, topic
            ORDER BY total DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    if not rows:
        return await _stance_sources_from_claims(conn, lock, topic, source_type, limit)

    sources_data = [
        {
            "source": r[0],
            "source_type": r[1],
            "topic": r[2],
            "supportive": int(r[3]),
            "critical": int(r[4]),
            "neutral": int(r[5]),
            "ambiguous": int(r[6]),
            "total": int(r[7]),
            "confidence": round(float(r[8]), 4) if r[8] is not None else None,
            "document_count": int(r[7]),
            "window_start": r[9],
            "window_end": r[10],
        }
        for r in rows
    ]
    return {"sources": sources_data, "count": len(sources_data)}


async def _stance_sources_from_claims(
    conn, lock, topic: Optional[str], source_type: Optional[str], limit: int
) -> Dict[str, Any]:
    """Fallback: derive per-source stances from argument_claims when source_stances is empty."""
    where_parts = ["1=1"]
    params: list[Any] = []
    if source_type:
        where_parts.append("c.source_type = ?")
        params.append(source_type)
    if topic:
        where_parts.append("n.category ILIKE ?")
        params.append(f"%{topic}%")
    params.append(limit * 4)

    with lock:
        rows = conn.execute(
            f"""
            WITH ev AS (
                SELECT claim_id,
                       SUM(CASE WHEN relation = 'supports'    THEN 1 ELSE 0 END) AS sup,
                       SUM(CASE WHEN relation = 'contradicts' THEN 1 ELSE 0 END) AS con
                FROM claim_evidence
                GROUP BY claim_id
            )
            SELECT
                COALESCE(n.source, c.source_type) AS source_name,
                c.source_type,
                COALESCE(n.category, 'general')   AS topic,
                c.confidence,
                COALESCE(ev.sup, 0)               AS sup,
                COALESCE(ev.con, 0)               AS con
            FROM argument_claims c
            LEFT JOIN news_articles n ON c.document_id = n.id
            LEFT JOIN ev             ON c.claim_id     = ev.claim_id
            WHERE {' AND '.join(where_parts)}
            ORDER BY n.publish_date DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()

    agg: dict[tuple, dict[str, Any]] = {}
    for source_name, stype, t, conf, sup, con in rows:
        key = (source_name, stype, t)
        if key not in agg:
            agg[key] = {"supportive": 0, "critical": 0, "neutral": 0, "ambiguous": 0, "total": 0}
        stance = _classify_stance(int(sup), int(con), float(conf or 0))
        agg[key][stance] += 1
        agg[key]["total"] += 1

    result = [
        {
            "source": k[0],
            "source_type": k[1],
            "topic": k[2],
            "supportive": v["supportive"],
            "critical": v["critical"],
            "neutral": v["neutral"],
            "ambiguous": v["ambiguous"],
            "total": v["total"],
            "confidence": None,
            "document_count": v["total"],
            "window_start": None,
            "window_end": None,
        }
        for k, v in sorted(agg.items(), key=lambda x: -x[1]["total"])
    ]
    return {"sources": result[:limit], "count": len(result)}


def _load_claim_stance_rows(conn, lock, source_type: Optional[str], source: Optional[str],
                             topic: Optional[str], cutoff: Optional[str]) -> list:
    """Load claims with derived stance labels from the DuckDB warehouse."""
    where_parts = ["1=1"]
    params: list[Any] = []

    if source_type:
        where_parts.append("c.source_type = ?")
        params.append(source_type)
    if source:
        where_parts.append("n.source ILIKE ?")
        params.append(f"%{source}%")
    if topic:
        where_parts.append("n.category ILIKE ?")
        params.append(f"%{topic}%")
    if cutoff:
        where_parts.append("n.publish_date >= ?")
        params.append(cutoff)

    query = f"""
    WITH evidence_counts AS (
        SELECT claim_id,
               SUM(CASE WHEN relation = 'supports'    THEN 1 ELSE 0 END) AS sup,
               SUM(CASE WHEN relation = 'contradicts' THEN 1 ELSE 0 END) AS con
        FROM claim_evidence
        GROUP BY claim_id
    )
    SELECT
        COALESCE(n.category, 'general') AS topic,
        c.source_type,
        c.confidence,
        c.extracted_at,
        COALESCE(ec.sup, 0)             AS sup,
        COALESCE(ec.con, 0)             AS con
    FROM argument_claims c
    LEFT JOIN news_articles n  ON c.document_id = n.id
    LEFT JOIN evidence_counts ec ON c.claim_id  = ec.claim_id
    WHERE {' AND '.join(where_parts)}
    """

    with lock:
        return conn.execute(query, params).fetchall()


@router.get("/stance/drift")
async def get_stance_drift(
    source: Optional[str] = Query(None, description="Filter by publication source name"),
    source_type: Optional[str] = Query(None, description="Filter by source type"),
    topic: Optional[str] = Query(None, description="Filter by topic/category keyword"),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """
    Return stance drift data for (source, topic).

    Primary response: structured drift events from `stance_drift_events` (populated
    nightly by the drift detection job — issue #102).

    Also returns a 7-bucket supportive-ratio time series derived from claim rows
    for the sparkline in the Stance panel (backward compat).
    """
    import threading
    from datetime import datetime

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    # ── Structured drift events from the dedicated table ─────────────────────
    events = _load_drift_events(conn, lock, source=source, source_type=source_type,
                                topic=topic, limit=limit)

    # ── Legacy 7-bucket time-series (sparkline) ───────────────────────────────
    rows = _load_claim_stance_rows(conn, lock, source_type, source, topic, cutoff=None)

    def _parse_ts(s: str) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    timed = [(r[3], r[2], int(r[4]), int(r[5])) for r in rows if r[3]]
    drift: list[float] = []
    periods: list[str] = []

    if timed:
        ts_objs = [_parse_ts(r[0]) for r in timed]
        valid = [(ts, r[1], r[2], r[3]) for ts, r in zip(ts_objs, timed) if ts is not None]
        if valid:
            t_min = min(v[0] for v in valid)
            t_max = max(v[0] for v in valid)
            span = max((t_max - t_min).total_seconds(), 1)
            n_buckets = 7
            buckets: list[dict] = [{"supportive": 0, "total": 0} for _ in range(n_buckets)]
            periods = [
                (t_min + (t_max - t_min) * (i / n_buckets)).strftime("%Y-%m-%d")
                for i in range(n_buckets)
            ]
            for ts, confidence, sup, con in valid:
                frac = (ts - t_min).total_seconds() / span
                idx = min(n_buckets - 1, int(frac * n_buckets))
                buckets[idx]["total"] += 1
                if _classify_stance(sup, con, float(confidence or 0)) == "supportive":
                    buckets[idx]["supportive"] += 1
            drift = [
                round(b["supportive"] / b["total"], 3) if b["total"] > 0 else 0.0
                for b in buckets
            ]

    return {
        "events": events,
        "drift": drift,
        "periods": periods,
        "topic": topic,
        "source": source,
        "source_type": source_type,
        "count": len(events),
    }


def _load_drift_events(
    conn,
    lock,
    *,
    source: Optional[str],
    source_type: Optional[str],
    topic: Optional[str],
    limit: int,
) -> list[dict]:
    """Query stance_drift_events filtered by source/source_type/topic."""
    conditions = []
    params: list = []
    if source:
        conditions.append("source = ?")
        params.append(source)
    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)
    if topic:
        conditions.append("topic = ?")
        params.append(topic)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    try:
        with lock:
            rows = conn.execute(
                f"""
                SELECT source, source_type, topic, from_stance, to_stance,
                       confidence_delta, detected_at, window_pair
                FROM stance_drift_events
                {where}
                ORDER BY window_pair DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
    except Exception:
        return []

    return [
        {
            "source": r[0],
            "source_type": r[1],
            "topic": r[2],
            "from_stance": r[3],
            "to_stance": r[4],
            "confidence_delta": r[5],
            "detected_at": r[6],
            "window_pair": r[7],
        }
        for r in rows
    ]


@router.get("/stance")
async def get_stance(
    topic: Optional[str] = Query(None, description="Filter by topic keyword (matches news category)"),
    source: Optional[str] = Query(None, description="Filter by publication source name"),
    source_type: Optional[str] = Query(None, description="Filter by source type"),
    date_range: Optional[str] = Query(None, description="Time window: 7d, 30d, 90d"),
    limit: int = Query(20, ge=1, le=200),
) -> Dict[str, Any]:
    """
    Return stance distribution (supportive/critical/neutral/ambiguous) across topics,
    derived from claim confidence and cross-corpus evidence relations.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    cutoff = _parse_date_cutoff(date_range)
    rows = _load_claim_stance_rows(conn, lock, source_type, source, topic, cutoff)

    by_topic: dict[str, Any] = {}

    for row in rows:
        cat, src_type, confidence, extracted_at, sup, con = row
        if cat not in by_topic:
            by_topic[cat] = {
                "supportive": 0, "critical": 0, "neutral": 0, "ambiguous": 0,
                "by_source": {}, "extracted_ats": [],
            }
        entry: Any = by_topic[cat]
        stance = _classify_stance(int(sup), int(con), float(confidence or 0))
        entry[stance] += 1
        if src_type not in entry["by_source"]:
            entry["by_source"][src_type] = {"supportive": 0, "critical": 0, "neutral": 0, "ambiguous": 0}
        entry["by_source"][src_type][stance] += 1
        if extracted_at:
            entry["extracted_ats"].append(extracted_at)

    def _total(d: Any) -> int:
        return d["supportive"] + d["critical"] + d["neutral"] + d["ambiguous"]

    result = []
    for topic_name, data in sorted(by_topic.items(), key=lambda x: -_total(x[1])):
        total = data["supportive"] + data["critical"] + data["neutral"] + data["ambiguous"]
        # Simple 7-point drift: interpolate supportive ratio across extracted_at range
        n_ts = len(data["extracted_ats"])
        base = round(data["supportive"] / max(total, 1), 2)
        drift = [base] * 7 if n_ts else []
        result.append({
            "topic": topic_name,
            "supportive": data["supportive"],
            "critical": data["critical"],
            "neutral": data["neutral"],
            "ambiguous": data["ambiguous"],
            "total": total,
            "drift": drift,
            "by_source": {k: dict(v) for k, v in data["by_source"].items()},
        })

    return {"stances": result[:limit], "count": len(result)}


# ---------------------------------------------------------------------------
# Frames/source endpoint (#95, enhanced by #105)
# ---------------------------------------------------------------------------

_CONCENTRATED_THRESHOLD = 0.60  # single frame dominance above this → concentrated framing flag


@router.get("/frames/source")
async def get_frames_by_source(
    source: Optional[str] = Query(None, description="Filter by publication source name"),
    source_type: Optional[str] = Query(None, description="Filter by source type"),
    topic: Optional[str] = Query(None, description="Filter by topic/category keyword"),
    date_range: Optional[str] = Query(None, description="Time window: 7d, 30d, 90d"),
    limit: int = Query(20, ge=1, le=200, description="Max number of sources to return"),
) -> Dict[str, Any]:
    """
    Return frame distribution aggregated per publication source, across all content types (#105).

    Each entry includes:
    - source: publication name (falls back to source_type for non-news when no name is available)
    - source_type: content type (news/blog/paper/transcript/book/note)
    - frames: avg score per frame label
    - doc_count: number of documents classified
    - dominant: highest-scoring frame
    - concentrated: True when any single frame avg_score > 60% (editorial framing signal)
    - concentrated_frame: name of the concentrated frame, or null
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    cutoff = _parse_date_cutoff(date_range)

    # ── news branch: join news_articles for source names ──────────────────────
    news_where: list[str] = ["n.source IS NOT NULL"]
    news_params: list[Any] = []
    if source_type and source_type != "news":
        news_where.append("1=0")  # exclude news entirely when filtering for non-news
    elif source_type == "news":
        pass  # include all news
    if source:
        news_where.append("n.source ILIKE ?")
        news_params.append(f"%{source}%")
    if topic:
        news_where.append("n.category ILIKE ?")
        news_params.append(f"%{topic}%")
    if cutoff:
        news_where.append("n.publish_date >= ?")
        news_params.append(cutoff)

    # ── non-news branch: fall back to source_type as source name ─────────────
    # (doc source names for non-news are available in source_stances when populated)
    other_where: list[str] = ["df.source_type != 'news'"]
    other_params: list[Any] = []
    if source_type and source_type != "news":
        other_where.append("df.source_type = ?")
        other_params.append(source_type)
    elif source_type == "news":
        other_where.append("1=0")  # exclude non-news when filtering for news only
    if cutoff:
        other_where.append("df.classified_at >= ?")
        other_params.append(cutoff)

    news_where_clause = " AND ".join(news_where)
    other_where_clause = " AND ".join(other_where)

    with lock:
        news_rows = conn.execute(
            f"""
            SELECT n.source,
                   'news'            AS source_type,
                   df.frame,
                   AVG(df.score)                  AS avg_score,
                   COUNT(DISTINCT df.document_id) AS doc_count
            FROM document_frames df
            JOIN news_articles n ON df.document_id = n.id
            WHERE {news_where_clause}
            GROUP BY n.source, df.frame
            """,
            news_params,
        ).fetchall()

        other_rows = conn.execute(
            f"""
            SELECT df.source_type      AS source,
                   df.source_type,
                   df.frame,
                   AVG(df.score)                  AS avg_score,
                   COUNT(DISTINCT df.document_id) AS doc_count
            FROM document_frames df
            WHERE {other_where_clause}
            GROUP BY df.source_type, df.frame
            """,
            other_params,
        ).fetchall()

    by_source: dict[str, Any] = {}
    for row in list(news_rows) + list(other_rows):
        src, src_type, frame, avg_score, doc_count = row
        key = f"{src_type}::{src}"
        if key not in by_source:
            by_source[key] = {"source": src, "source_type": src_type, "frames": {}, "doc_count": 0}
        entry_s: Any = by_source[key]
        entry_s["frames"][frame] = round(float(avg_score), 4)
        entry_s["doc_count"] = max(entry_s["doc_count"], int(doc_count))

    result = []
    for data in sorted(by_source.values(), key=lambda x: -x["doc_count"]):
        frames = data["frames"]
        dominant = max(frames, key=frames.__getitem__) if frames else "other"
        top_score = frames.get(dominant, 0.0)
        concentrated = top_score > _CONCENTRATED_THRESHOLD
        result.append({
            "source": data["source"],
            "source_type": data["source_type"],
            "frames": frames,
            "doc_count": data["doc_count"],
            "dominant": dominant,
            "concentrated": concentrated,
            "concentrated_frame": dominant if concentrated else None,
        })

    # Apply name-based filter after aggregation (handles fallback source names)
    if source:
        result = [r for r in result if source.lower() in r["source"].lower()]

    return {"sources": result[:limit], "count": len(result)}


# ---------------------------------------------------------------------------
# Positions endpoint (#95)
# ---------------------------------------------------------------------------

@router.get("/positions")
async def get_positions(
    actor: Optional[str] = Query(None, description="Filter by actor name (ILIKE)"),
    topic: Optional[str] = Query(None, description="Filter by topic keyword (ILIKE)"),
    source_type: Optional[str] = Query(None, description="Filter by source type"),
    date_range: Optional[str] = Query(None, description="Time window: 7d, 30d, 90d"),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """
    Return actor policy positions from the ``policy_positions`` table (#110).

    Each position is a sentence where a named actor asserts an intention or
    commitment on a policy topic. Results come from
    ``src.argument_mining.positions.extract_positions`` run as a post-ingestion
    stage. Falls back to claim-derived positions when the table is empty.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    date_cutoff = _parse_date_cutoff(date_range)
    where_parts: list[str] = []
    params: list[Any] = []

    if source_type:
        where_parts.append("source_type = ?")
        params.append(source_type)
    if actor:
        where_parts.append("actor ILIKE ?")
        params.append(f"%{actor}%")
    if topic:
        where_parts.append("topic ILIKE ?")
        params.append(f"%{topic}%")
    if date_cutoff:
        where_parts.append("position_date >= ?")
        params.append(date_cutoff)

    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)

    with lock:
        # Try the dedicated policy_positions table first
        try:
            rows = conn.execute(
                f"""
                SELECT position_id, actor, topic, position_text,
                       source_type, document_id, position_date, confidence
                FROM policy_positions
                {where_clause}
                ORDER BY position_date DESC NULLS LAST, confidence DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        except Exception:
            rows = []

        # If table is empty, fall back to claim-derived positions
        if not rows:
            fb_where_parts = []
            fb_params: list[Any] = []
            if source_type:
                fb_where_parts.append("c.source_type = ?")
                fb_params.append(source_type)
            if actor:
                fb_where_parts.append("n.source ILIKE ?")
                fb_params.append(f"%{actor}%")
            if topic:
                fb_where_parts.append("n.category ILIKE ?")
                fb_params.append(f"%{topic}%")
            fb_where = ("WHERE " + " AND ".join(fb_where_parts)) if fb_where_parts else ""
            fb_params.append(limit)
            try:
                fb_rows = conn.execute(
                    f"""
                    WITH evidence_counts AS (
                        SELECT claim_id,
                               SUM(CASE WHEN relation = 'supports'    THEN 1 ELSE 0 END) AS sup,
                               SUM(CASE WHEN relation = 'contradicts' THEN 1 ELSE 0 END) AS con
                        FROM claim_evidence
                        GROUP BY claim_id
                    )
                    SELECT
                        c.claim_id,
                        COALESCE(n.source, c.source_type)   AS actor,
                        COALESCE(n.category, 'general')     AS topic,
                        c.claim_text,
                        c.source_type,
                        c.document_id,
                        n.publish_date,
                        c.confidence
                    FROM argument_claims c
                    LEFT JOIN news_articles n    ON c.document_id = n.id
                    LEFT JOIN evidence_counts ec ON c.claim_id   = ec.claim_id
                    {fb_where}
                    ORDER BY n.publish_date DESC NULLS LAST, c.confidence DESC
                    LIMIT ?
                    """,
                    fb_params,
                ).fetchall()
                return {
                    "positions": [
                        {
                            "position_id": r[0],
                            "actor": r[1] or r[4],
                            "topic": r[2],
                            "position": r[3],
                            "source_type": r[4],
                            "document_id": r[5],
                            "date": str(r[6]) if r[6] else "",
                            "confidence": round(float(r[7] or 0), 4),
                            "updates": [],
                            "_source": "claims_fallback",
                        }
                        for r in fb_rows
                    ],
                    "count": len(fb_rows),
                    "_note": "policy_positions table is empty — run /positions/extract first",
                }
            except Exception:
                return {"positions": [], "count": 0}

        # Fetch follow-through updates for all returned positions
        position_ids = [r[0] for r in rows]
        placeholders = ", ".join("?" * len(position_ids))
        try:
            update_rows = conn.execute(
                f"""
                SELECT update_id, position_id, article_id, update_type,
                       evidence_text, confidence, detected_at
                FROM position_updates
                WHERE position_id IN ({placeholders})
                ORDER BY detected_at ASC
                """,
                position_ids,
            ).fetchall()
        except Exception:
            update_rows = []

    # Group updates by position_id
    updates_by_pos: dict[str, list] = {}
    for u in update_rows:
        pid = u[1]
        updates_by_pos.setdefault(pid, []).append({
            "update_id":     u[0],
            "article_id":    u[2],
            "update_type":   u[3],
            "evidence_text": u[4] or "",
            "confidence":    round(float(u[5] or 0), 4),
            "detected_at":   u[6] or "",
        })

    positions = [
        {
            "position_id": row[0],
            "actor":        row[1],
            "topic":        row[2],
            "position":     row[3],
            "source_type":  row[4],
            "document_id":  row[5],
            "date":         row[6] or "",
            "confidence":   round(float(row[7] or 0), 4),
            "updates":      updates_by_pos.get(row[0], []),
        }
        for row in rows
    ]
    return {"positions": positions, "count": len(positions)}


@router.post("/positions/extract")
async def extract_positions_for_document(
    document_id: str = Query(..., description="ID of a document already in news_articles"),
    source_type: str = Query("news", description="Source type label"),
) -> Dict[str, Any]:
    """
    Run position extraction on a stored document and persist results (#110).

    Identifies sentences where a named actor asserts a commitment on a policy
    topic and stores them in ``policy_positions``. Runs
    ``src.argument_mining.positions.run_position_pipeline`` which also calls
    ``extract_positions`` internally.
    """
    import threading
    import time

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    with lock:
        row = conn.execute(
            "SELECT id, title, content, publish_date, source, category "
            "FROM news_articles WHERE id = ? LIMIT 1",
            [document_id],
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found")

    doc_id, title, content, publish_date, source, category = row
    if not content:
        raise HTTPException(status_code=422, detail="Document has no content to process")

    try:
        from services.ingest.common.document_model import Document
        from src.argument_mining.positions import run_position_pipeline

        metadata: Dict[str, Any] = {}
        if category:
            metadata["category"] = category

        created_at_ms: Optional[int] = None
        if publish_date is not None:
            try:
                created_at_ms = int(publish_date.timestamp() * 1000)
            except (AttributeError, OSError, ValueError):
                pass

        doc = Document(
            document_id=doc_id,
            source_type=source_type,
            language="en",
            ingested_at=int(time.time() * 1000),
            source_id=source,
            title=title,
            content=content,
            created_at=created_at_ms,
            metadata=metadata,
        )

        with lock:
            records = run_position_pipeline(doc, conn)

        return {
            "document_id": doc_id,
            "source_type": source_type,
            "positions_extracted": len(records),
            "positions": [
                {
                    "position_id": r.position_id,
                    "actor": r.actor,
                    "topic": r.topic,
                    "position_text": r.position_text,
                    "date": r.position_date or "",
                    "confidence": r.confidence,
                }
                for r in records
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {exc}")


# ---------------------------------------------------------------------------
# Position follow-through check (#111)
# ---------------------------------------------------------------------------

@router.post("/positions/{position_id}/check")
async def check_position_followthrough(
    position_id: str,
    limit: int = Query(100, ge=1, le=500, description="Max articles to scan"),
) -> Dict[str, Any]:
    """
    Trigger an on-demand follow-through check for a single position (#111).

    Scans recent articles for references to the same (actor, topic) pair and
    classifies each reference as reaffirmed / reversed / updated / no_signal.
    Results are stored in ``position_updates`` and returned immediately.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection
    from src.argument_mining.position_tracker import (
        check_position_followthrough as _check,
        store_followthrough,
    )

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    with lock:
        pos_row = conn.execute(
            "SELECT actor, topic, extracted_at FROM policy_positions WHERE position_id = ? LIMIT 1",
            [position_id],
        ).fetchone()

    if not pos_row:
        raise HTTPException(status_code=404, detail=f"Position '{position_id}' not found")

    actor, topic, extracted_at = pos_row
    since = (extracted_at or "1970-01-01")[:10]

    with lock:
        articles = conn.execute(
            "SELECT id, content, publish_date FROM news_articles "
            "WHERE CAST(publish_date AS VARCHAR) > ? LIMIT ?",
            [since, limit],
        ).fetchall()

    try:
        records = _check(position_id, actor, topic, articles)
        if records:
            with lock:
                store_followthrough(records, conn)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Follow-through check failed: {exc}")

    summary: Dict[str, int] = {}
    for r in records:
        summary[r.update_type] = summary.get(r.update_type, 0) + 1

    return {
        "position_id": position_id,
        "actor": actor,
        "topic": topic,
        "articles_scanned": len(articles),
        "updates_found": len(records),
        "summary": summary,
        "updates": [
            {
                "update_id":     r.update_id,
                "article_id":    r.article_id,
                "update_type":   r.update_type,
                "evidence_text": r.evidence_text,
                "confidence":    r.confidence,
                "detected_at":   r.detected_at,
            }
            for r in records
        ],
    }


# ---------------------------------------------------------------------------
# Controversy endpoint (#95)
# ---------------------------------------------------------------------------

@router.get("/controversy")
async def get_controversy(
    topic: Optional[str] = Query(None, description="Filter by topic/category keyword"),
    source_type: Optional[str] = Query(None, description="Filter by source type"),
    date_range: Optional[str] = Query(None, description="Time window: 7d, 30d, 90d"),
    limit: int = Query(20, ge=1, le=200),
) -> Dict[str, Any]:
    """
    Return conflict pairs ranked by similarity score (#112).

    Prefers the ``claim_conflicts`` table populated by the semantic-similarity
    batch job (``POST /controversy/compute``).  Falls back to deriving pairs
    from ``claim_evidence`` contradictions when the table is empty.

    Intensity is normalised to [0, 1] within the result set.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    cutoff = _parse_date_cutoff(date_range)

    # ── Primary: claim_conflicts ──────────────────────────────────────────────
    cf_where: list[str] = []
    cf_params: list[Any] = []
    if source_type:
        cf_where.append("(cf.source_type_a = ? OR cf.source_type_b = ?)")
        cf_params.extend([source_type, source_type])
    if topic:
        cf_where.append("cf.topic ILIKE ?")
        cf_params.append(f"%{topic}%")
    if cutoff:
        cf_where.append("cf.computed_at >= ?")
        cf_params.append(cutoff)
    cf_where_clause = ("WHERE " + " AND ".join(cf_where)) if cf_where else ""
    cf_params.append(limit * 3)

    with lock:
        try:
            cf_rows = conn.execute(
                f"""
                SELECT
                    COALESCE(na.source, ca.source_type) AS actor_a,
                    COALESCE(nb.source, cb.source_type) AS actor_b,
                    cf.topic,
                    cf.similarity_score,
                    cf.conflict_type,
                    cf.source_type_a,
                    cf.source_type_b
                FROM claim_conflicts cf
                JOIN argument_claims ca ON cf.claim_id_a = ca.claim_id
                JOIN argument_claims cb ON cf.claim_id_b = cb.claim_id
                LEFT JOIN news_articles na ON ca.document_id = na.id
                LEFT JOIN news_articles nb ON cb.document_id = nb.id
                {cf_where_clause}
                ORDER BY cf.similarity_score DESC
                LIMIT ?
                """,
                cf_params,
            ).fetchall()
        except Exception:
            cf_rows = []

    if cf_rows:
        max_sim = max(r[3] or 0 for r in cf_rows) or 1.0
        conflicts = [
            {
                "actor_a":      r[0],
                "actor_b":      r[1],
                "topic":        r[2],
                "intensity":    round((r[3] or 0) / max_sim, 2),
                "source_count": 1,
                "conflict_type": r[4],
                "source_type_a": r[5],
                "source_type_b": r[6],
            }
            for r in cf_rows[:limit]
        ]
        return {"conflicts": conflicts, "count": len(conflicts), "_source": "claim_conflicts"}

    # ── Fallback: claim_evidence contradictions ───────────────────────────────
    fb_where = [
        "e.relation = 'contradicts'",
        "n1.source IS NOT NULL",
        "n2.source IS NOT NULL",
        "n1.source != n2.source",
    ]
    fb_params: list[Any] = []
    if source_type:
        fb_where.append("c.source_type = ?")
        fb_params.append(source_type)
    if topic:
        fb_where.append("n1.category ILIKE ?")
        fb_params.append(f"%{topic}%")
    if cutoff:
        fb_where.append("n1.publish_date >= ?")
        fb_params.append(cutoff)
    fb_params.append(limit * 3)

    with lock:
        rows = conn.execute(
            f"""
            SELECT
                n1.source                        AS actor_a,
                n2.source                        AS actor_b,
                COALESCE(n1.category, 'general') AS topic,
                COUNT(*)                         AS contradiction_count,
                COUNT(DISTINCT n2.id)            AS source_count
            FROM claim_evidence e
            JOIN argument_claims c   ON e.claim_id            = c.claim_id
            JOIN news_articles n1    ON c.document_id          = n1.id
            JOIN news_articles n2    ON e.evidence_document_id = n2.id
            WHERE {' AND '.join(fb_where)}
            GROUP BY n1.source, n2.source, COALESCE(n1.category, 'general')
            ORDER BY contradiction_count DESC
            LIMIT ?
            """,
            fb_params,
        ).fetchall()

    if not rows:
        return {"conflicts": [], "count": 0}

    max_count = max(r[3] for r in rows)
    conflicts = [
        {
            "actor_a": r[0],
            "actor_b": r[1],
            "topic": r[2],
            "intensity": round(r[3] / max_count, 2),
            "source_count": r[4],
        }
        for r in rows[:limit]
    ]
    return {"conflicts": conflicts, "count": len(conflicts), "_source": "claim_evidence_fallback"}


# ---------------------------------------------------------------------------
# Controversy graph endpoint (#96, #112)
# ---------------------------------------------------------------------------

@router.get("/controversy/graph")
async def get_controversy_graph(
    topic: Optional[str] = Query(None, description="Filter by topic/category keyword"),
    source_type: Optional[str] = Query(None, description="Filter by source type"),
    date_range: Optional[str] = Query(None, description="Time window: 7d, 30d, 90d"),
    limit: int = Query(60, ge=1, le=300),
) -> Dict[str, Any]:
    """
    Return a force-directed graph of contested claims (#96, #112).

    Prefers ``claim_conflicts`` (semantic-similarity conflicts) populated by
    ``POST /controversy/compute``.  Falls back to ``claim_evidence`` contradictions.

    Node fields:  id, label, source, source_type, topic, date, claim_text,
                  confidence, document_id, conflict_type
    Edge fields:  source, target, severity (similarity ∈ [0,1]), relation,
                  conflict_type
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection
    from src.argument_mining.conflict_graph import build_conflict_graph

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()
    cutoff = _parse_date_cutoff(date_range)

    result = build_conflict_graph(conn, lock, topic=topic, source_type=source_type,
                                  date_cutoff=cutoff, limit=limit)

    # If claim_conflicts is empty, fall back to old claim_evidence query
    if result["node_count"] == 0:
        conditions: list[str] = [
            "e.relation = 'contradicts'",
            "n1.source IS NOT NULL",
            "n2.source IS NOT NULL",
            "n1.source != n2.source",
        ]
        fb_params: list[Any] = []
        if source_type:
            conditions.append("(c1.source_type = ? OR c2.source_type = ?)")
            fb_params.extend([source_type, source_type])
        if topic:
            conditions.append("(n1.category ILIKE ? OR n2.category ILIKE ?)")
            fb_params.extend([f"%{topic}%", f"%{topic}%"])
        if cutoff:
            conditions.append("(n1.publish_date >= ? OR n2.publish_date >= ?)")
            fb_params.extend([cutoff, cutoff])
        fb_params.append(limit)

        with lock:
            rows = conn.execute(
                f"""
                SELECT
                    c1.claim_id, c1.claim_text, c1.source_type,
                    c1.confidence, c1.extracted_at,
                    COALESCE(n1.source, c1.source_type), COALESCE(n1.category, 'general'),
                    c1.document_id,
                    c2.claim_id, c2.claim_text, c2.source_type,
                    c2.confidence, c2.extracted_at,
                    COALESCE(n2.source, c2.source_type), COALESCE(n2.category, 'general'),
                    c2.document_id,
                    COALESCE(e.similarity_score, 0.5)
                FROM claim_evidence e
                JOIN argument_claims c1 ON e.claim_id             = c1.claim_id
                JOIN argument_claims c2 ON e.evidence_document_id = c2.document_id
                LEFT JOIN news_articles n1 ON c1.document_id = n1.id
                LEFT JOIN news_articles n2 ON c2.document_id = n2.id
                WHERE {' AND '.join(conditions)}
                ORDER BY COALESCE(e.similarity_score, 0.5) DESC
                LIMIT ?
                """,
                fb_params,
            ).fetchall()

        nodes: dict[str, Any] = {}
        edges: list[dict[str, Any]] = []
        for r in rows:
            cid_a, txt_a, st_a, cf_a, dt_a, src_a, tp_a, doc_a, \
            cid_b, txt_b, st_b, cf_b, dt_b, src_b, tp_b, doc_b, sev = r
            for cid, txt, stype, conf, date_, src, tp, doc in [
                (cid_a, txt_a, st_a, cf_a, dt_a, src_a, tp_a, doc_a),
                (cid_b, txt_b, st_b, cf_b, dt_b, src_b, tp_b, doc_b),
            ]:
                if cid not in nodes:
                    nodes[cid] = {"id": cid, "label": src, "source": src,
                                  "source_type": stype, "topic": tp,
                                  "date": str(date_) if date_ else None,
                                  "claim_text": txt,
                                  "confidence": float(conf) if conf is not None else 0.5,
                                  "document_id": doc}
            edges.append({"source": cid_a, "target": cid_b,
                           "severity": round(float(sev), 3), "relation": "contradicts"})

        node_list = list(nodes.values())
        result = {"nodes": node_list, "edges": edges,
                  "node_count": len(node_list), "edge_count": len(edges),
                  "_source": "claim_evidence_fallback"}

    return result


# ---------------------------------------------------------------------------
# On-demand conflict computation (#112)
# ---------------------------------------------------------------------------

@router.post("/controversy/compute")
async def compute_conflicts(
    limit: int = Query(300, ge=1, le=1000, description="Max claims to scan"),
    date_range: Optional[str] = Query(None, description="Only scan claims since: 7d, 30d, 90d"),
) -> Dict[str, Any]:
    """
    Trigger on-demand conflict detection across stored claims (#112).

    Computes pairwise cosine similarity between claims in the same topic window,
    flags pairs with similarity ≥ 0.65 and opposing stance signals as conflicts
    (direct: ≥ 0.80 + explicit contradiction; implied: ≥ 0.65 + cross-format),
    and stores results in ``claim_conflicts``.

    Run nightly automatically via the APScheduler job in
    ``src.argument_mining.conflict_scheduler``.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection
    from src.argument_mining.conflict_graph import compute_claim_conflicts

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()
    cutoff = _parse_date_cutoff(date_range)

    try:
        counts = compute_claim_conflicts(conn, lock, limit=limit, date_range=cutoff)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Conflict computation failed: {exc}")

    return {"status": "ok", **counts}


# ---------------------------------------------------------------------------
# Sources ranking endpoint (#95)
# ---------------------------------------------------------------------------

@router.get("/sources/ranking")
async def get_sources_ranking(
    source_type: Optional[str] = Query(None, description="Filter by source type"),
    limit: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    """Rank content sources by claim count, average confidence, and evidence citation frequency."""
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    where_parts: list[str] = []
    params: list[Any] = []

    if source_type:
        where_parts.append("c.source_type = ?")
        params.append(source_type)

    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)

    with lock:
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(n.source, c.source_type)    AS source_name,
                c.source_type,
                COUNT(DISTINCT c.claim_id)           AS claim_count,
                ROUND(AVG(c.confidence), 4)          AS avg_confidence,
                COUNT(e.evidence_id)                 AS evidence_citations
            FROM argument_claims c
            LEFT JOIN news_articles n  ON c.document_id = n.id
            LEFT JOIN claim_evidence e ON c.claim_id    = e.claim_id
            {where_clause}
            GROUP BY COALESCE(n.source, c.source_type), c.source_type
            ORDER BY claim_count DESC, avg_confidence DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    sources = [
        {
            "source": r[0],
            "source_type": r[1],
            "claim_count": r[2],
            "avg_confidence": float(r[3]) if r[3] is not None else 0.0,
            "evidence_citations": r[4],
        }
        for r in rows
    ]

    return {"sources": sources, "count": len(sources)}


# ---------------------------------------------------------------------------
# Actor / source metadata endpoints (#114)
# ---------------------------------------------------------------------------

@router.get("/actors")
async def get_actors(
    document_id: Optional[str] = Query(None, description="Filter by document ID"),
    source_type: Optional[str] = Query(None, description="Filter by content type"),
    role: Optional[str] = Query(None, description="Filter by role: speaker|subject|author"),
    actor_name: Optional[str] = Query(None, description="Partial name match (ILIKE)"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
) -> Dict[str, Any]:
    """
    Query ``document_actors`` for extracted actor/source metadata.

    Each record carries a stable ``entity_id`` (sha1 of the canonical name)
    that joins to the entity graph without a separate entity table.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    conditions: list[str] = []
    params: list[Any] = []

    if document_id:
        conditions.append("document_id = ?")
        params.append(document_id)
    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)
    if role:
        conditions.append("role = ?")
        params.append(role)
    if actor_name:
        conditions.append("actor_name ILIKE ?")
        params.append(f"%{actor_name}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    with lock:
        rows = conn.execute(
            f"""
            SELECT document_id, source_type, actor_name, entity_id,
                   role, confidence, extracted_at
            FROM document_actors
            {where}
            ORDER BY confidence DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    actors = [
        {
            "document_id":  r[0],
            "source_type":  r[1],
            "actor_name":   r[2],
            "entity_id":    r[3],
            "role":         r[4],
            "confidence":   round(r[5], 4) if r[5] is not None else None,
            "extracted_at": r[6],
        }
        for r in rows
    ]
    return {"actors": actors, "count": len(actors)}


@router.post("/actors/extract")
async def extract_actors_for_document(
    document_id: str = Query(..., description="Document ID in news_articles"),
    source_type: str = Query("news", description="Source type of the document"),
) -> Dict[str, Any]:
    """
    Run actor extraction on a single stored document and persist results.

    Uses spaCy NER when ``en_core_web_sm`` is installed; falls back to
    regex heuristics automatically.
    """
    import threading, time

    from src.database.local_analytics_connector import get_shared_connection
    from src.argument_mining.metadata import extract_actors, store_actors

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    with lock:
        row = conn.execute(
            "SELECT id, title, content, source FROM news_articles WHERE id = ? LIMIT 1",
            [document_id],
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Document '{document_id}' not found")

    from services.ingest.common.document_model import Document  # type: ignore[import]

    doc = Document(
        document_id=row[0],
        source_type=source_type,
        language="en",
        ingested_at=int(time.time() * 1000),
        source_id=row[3],
        title=row[1],
        content=row[2] or "",
    )
    records = extract_actors(doc)
    if records:
        with lock:
            store_actors(records, conn)

    return {
        "document_id": document_id,
        "source_type": source_type,
        "actors_extracted": len(records),
        "actors": [
            {"actor_name": r.actor_name, "role": r.role,
             "entity_id": r.entity_id, "confidence": round(r.confidence, 4)}
            for r in records
        ],
    }


@router.post("/actors/batch")
async def run_actors_batch(
    limit: int = Query(200, ge=1, le=2000,
                       description="Max news_articles documents to process"),
) -> Dict[str, Any]:
    """
    Extract actors for news_articles documents not yet in ``document_actors``.

    Safe to call repeatedly — already-processed documents are skipped.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection
    from src.argument_mining.metadata import run_actor_batch

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()
    return run_actor_batch(conn, lock, limit=limit)


@router.get("/actors/summary")
async def get_actors_summary(
    source_type: Optional[str] = Query(None, description="Filter by content type"),
    role: Optional[str] = Query(None, description="Filter by role"),
    limit: int = Query(30, ge=1, le=200, description="Top-N actors by document frequency"),
) -> Dict[str, Any]:
    """
    Aggregate view: most frequent actors across all documents.

    Returns top actors by document_count, broken down by role.  Useful for
    feeding the stance detection and policy position pipelines with a
    pre-ranked actor list.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    conditions: list[str] = []
    params: list[Any] = []

    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)
    if role:
        conditions.append("role = ?")
        params.append(role)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    with lock:
        rows = conn.execute(
            f"""
            SELECT actor_name, entity_id, role,
                   COUNT(DISTINCT document_id) AS doc_count,
                   ROUND(AVG(confidence), 4)   AS avg_confidence
            FROM document_actors
            {where}
            GROUP BY actor_name, entity_id, role
            ORDER BY doc_count DESC, avg_confidence DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    return {
        "actors": [
            {
                "actor_name":     r[0],
                "entity_id":      r[1],
                "role":           r[2],
                "doc_count":      r[3],
                "avg_confidence": float(r[4]) if r[4] is not None else 0.0,
            }
            for r in rows
        ],
        "count": len(rows),
    }


# ---------------------------------------------------------------------------
# Outlet editorial-framing cluster endpoints (#115)
# ---------------------------------------------------------------------------

@router.get("/outlets/clusters")
async def get_outlet_clusters(
    source_type: Optional[str] = Query(None, description="Filter by content type"),
    cluster_id:  Optional[int] = Query(None, description="Filter by cluster ID"),
    limit: int = Query(200, ge=1, le=1000),
) -> Dict[str, Any]:
    """
    Return stored outlet cluster assignments with PCA 2D coordinates.

    Each record includes source name, source_type, cluster_id, descriptive
    cluster_label, pca_x/pca_y scatter coordinates, dominant_frame, and
    doc_count.  Coordinates are in PCA space (arbitrary scale, centered near 0).

    Run ``POST /outlets/cluster`` first if the table is empty.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    conditions: list[str] = []
    params: list[Any] = []
    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)
    if cluster_id is not None:
        conditions.append("cluster_id = ?")
        params.append(cluster_id)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    with lock:
        rows = conn.execute(
            f"""
            SELECT source, source_type, cluster_id, cluster_label,
                   pca_x, pca_y, dominant_frame, doc_count, computed_at
            FROM outlet_clusters
            {where}
            ORDER BY cluster_id, doc_count DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    outlets = [
        {
            "source":        r[0],
            "source_type":   r[1],
            "cluster_id":    r[2],
            "cluster_label": r[3],
            "pca_x":         round(r[4], 4) if r[4] is not None else 0.0,
            "pca_y":         round(r[5], 4) if r[5] is not None else 0.0,
            "dominant_frame": r[6],
            "doc_count":     r[7],
            "computed_at":   r[8],
        }
        for r in rows
    ]
    return {"outlets": outlets, "count": len(outlets)}


@router.post("/outlets/cluster")
async def trigger_outlet_clustering(
    date_range: str = Query("90d", description="Frame aggregation window: 7d|30d|90d|180d|365d"),
    k_min: int = Query(2, ge=2, le=5,  description="Minimum number of clusters to try"),
    k_max: int = Query(8, ge=2, le=12, description="Maximum number of clusters to try"),
) -> Dict[str, Any]:
    """
    Embed outlets as frame vectors, run k-means + hierarchical clustering (k_min..k_max),
    select best k via silhouette score, project to 2D via PCA, and persist results.

    Safe to call repeatedly — each call overwrites the previous run.

    Returns a summary: n_outlets, chosen k, method, silhouette score.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection
    from src.argument_mining.outlet_clustering import run_cluster_pipeline

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()
    return run_cluster_pipeline(conn, lock, date_range=date_range, k_min=k_min, k_max=k_max)


# ---------------------------------------------------------------------------
# Outlet transparency scoring endpoints (#116)
# ---------------------------------------------------------------------------

@router.get("/outlets/ranking")
async def get_outlets_ranking(
    source_type: Optional[str] = Query(None, description="Filter by content type"),
    sort_by: str = Query("composite_score",
                         description="Sort key: composite_score|frame_diversity|attribution_rate|stance_neutrality"),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """
    Return outlets ranked by transparency score with weekly sparkline history.

    Each record includes the latest scores for all three dimensions plus a
    ``trend`` array (up to 8 weekly composite_score snapshots, oldest first)
    suitable for rendering as a sparkline.

    Run ``POST /outlets/score`` first to populate ``outlet_scores``.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection

    _SORT_COLS = {"composite_score", "frame_diversity", "attribution_rate", "stance_neutrality"}
    sort_col = sort_by if sort_by in _SORT_COLS else "composite_score"

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()

    conditions: list[str] = []
    params: list[Any] = []
    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)

    where = f"AND {' AND '.join(conditions)}" if conditions else ""

    # Latest snapshot per outlet
    params.append(limit)
    with lock:
        latest = conn.execute(f"""
            SELECT source, source_type, score_date,
                   frame_diversity, attribution_rate, stance_neutrality,
                   composite_score, doc_count, claim_count, computed_at
            FROM outlet_scores os
            WHERE (source, source_type, score_date) IN (
                SELECT source, source_type, MAX(score_date)
                FROM outlet_scores
                WHERE 1=1 {where}
                GROUP BY source, source_type
            )
            ORDER BY {sort_col} DESC NULLS LAST
            LIMIT ?
        """, params).fetchall()

    if not latest:
        return {"outlets": [], "count": 0}

    # Fetch trend history for all returned outlets
    outlet_keys = [(r[0], r[1]) for r in latest]
    # Build a single query for all history rows
    placeholders = ", ".join(["(?, ?)"] * len(outlet_keys))
    flat_keys = [v for pair in outlet_keys for v in pair]
    with lock:
        history_rows = conn.execute(f"""
            SELECT source, source_type, score_date, composite_score
            FROM outlet_scores
            WHERE (source, source_type) IN ({placeholders})
            ORDER BY source, source_type, score_date
        """, flat_keys).fetchall()

    # Group history by (source, source_type)
    trends: Dict[tuple, list] = {}
    for r in history_rows:
        key = (r[0], r[1])
        trends.setdefault(key, []).append(float(r[3]) if r[3] is not None else 0.0)

    outlets = []
    for rank, r in enumerate(latest, start=1):
        key = (r[0], r[1])
        trend = trends.get(key, [float(r[6]) if r[6] is not None else 0.0])
        outlets.append({
            "rank":             rank,
            "source":           r[0],
            "source_type":      r[1],
            "score_date":       r[2],
            "frame_diversity":  round(r[3], 4) if r[3] is not None else None,
            "attribution_rate": round(r[4], 4) if r[4] is not None else None,
            "stance_neutrality": round(r[5], 4) if r[5] is not None else None,
            "composite_score":  round(r[6], 4) if r[6] is not None else None,
            "doc_count":        r[7],
            "claim_count":      r[8],
            "trend":            trend,
        })

    return {"outlets": outlets, "count": len(outlets)}


@router.post("/outlets/score")
async def trigger_outlet_scoring(
    date_range: str = Query("90d",
                            description="Data window for computing scores: 7d|30d|90d|180d|365d"),
) -> Dict[str, Any]:
    """
    Compute weekly transparency scores for all outlets and persist to ``outlet_scores``.

    Idempotent — re-running in the same ISO week overwrites that week's row.

    Returns: outlets_scored, score_date, top_outlet, top_composite.
    """
    import threading

    from src.database.local_analytics_connector import get_shared_connection
    from src.argument_mining.outlet_scorer import run_scorer_batch

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()
    return run_scorer_batch(conn, lock, date_range=date_range)
