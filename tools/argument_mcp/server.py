"""
NeuroNews Argument-mining inspector — MCP server.

Token-efficient read-only access to every argument-mining table in the local
DuckDB warehouse.  Designed to replace hand-written SQL in debugging sessions.

Tools (non-overlapping with pipeline_mcp which owns positions/conflicts):
  am_stats()                              -> row counts for all AM tables
  list_claims(source_type?, topic?,       -> compact claim list with text preview
              factchecked?, limit?)
  list_stances(source?, topic?,           -> per-source stance aggregation rows
               stance?, limit?)
  list_drift_events(source?, topic?,      -> stance-drift timeline entries
                    limit?)
  list_frames(source_type?, frame?,       -> frame distribution rows from
              limit?)                        document_frames
  claim_evidence_pairs(claim_id?,         -> claim-to-claim evidence pairs
                       relation?, limit?)
  list_unsourced_claims(source_type?,     -> unattributed claim records
                        limit?)
  trigger_attribution_batch(limit?)       -> classify unattributed claims (RW)
  list_actors(document_id?, source_type?, -> actor/source metadata rows
              role?, actor_name?, limit?)
  actor_summary(source_type?, role?,      -> top actors by document frequency
                limit?)
  trigger_actor_batch(limit?)             -> extract actors from news_articles (RW)
  list_outlet_clusters(source_type?,      -> stored outlet cluster assignments
                       cluster_id?)
  trigger_outlet_clustering(date_range?,  -> embed+cluster outlets, persist (RW)
                            k_min?, k_max?)
  list_outlet_scores(source_type?,       -> latest transparency scores per outlet
                     sort_by?, limit?)
  trigger_outlet_scoring(date_range?)    -> compute+store weekly scores (RW)
  get_benchmark_results(model?)         -> read docs/benchmark_results.json (no inference)

Design constraints (same as pipeline_mcp):
  * Lazy imports inside each tool — top-level imports are stdlib + fastmcp only.
  * DuckDB opened READ-ONLY so we never conflict with the API writer lock.
  * All results are capped summaries, never full payloads.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Stdlib-only helper for the analytics honesty contract (R5); safe at import.
from src.analytics.honesty import INTERVAL_SCHEMA, honesty_output_schema  # noqa: E402

mcp = FastMCP("neuronews-arguments")

MAX_LIST = 30
CLAIM_PREVIEW = 120


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _warehouse_ro():
    """Open the DuckDB warehouse read-only, honouring NEURONEWS_DB_PATH."""
    import os
    import duckdb
    import tempfile
    import shutil

    db_path = os.environ.get(
        "NEURONEWS_DB_PATH",
        str(REPO_ROOT / "data" / "local_warehouse.duckdb"),
    )
    try:
        conn = duckdb.connect(db_path, read_only=True)
        return conn, None
    except Exception as e:
        if "read_only" in str(e).lower() or "lock" in str(e).lower():
            return None, f"warehouse locked by writer: {e}"
        # Warehouse may not exist yet — seed a temp copy for introspection
        fd, tmp = tempfile.mkstemp(suffix=".duckdb")
        os.close(fd)
        if Path(db_path).exists():
            shutil.copy2(db_path, tmp)
        try:
            from src.database.local_warehouse_seed import seed_warehouse  # type: ignore[import]
            conn2 = duckdb.connect(tmp, read_only=False)
            seed_warehouse(conn2)
            return conn2, None
        except Exception as e2:
            return None, f"cannot open warehouse: {e2}"


def _clip(text: str, n: int = CLAIM_PREVIEW) -> str:
    if not text:
        return ""
    return text[:n] + ("…" if len(text) > n else "")


# --------------------------------------------------------------------------- #
# Tools                                                                        #
# --------------------------------------------------------------------------- #

@mcp.tool
def am_stats() -> dict:
    """
    Row counts for every argument-mining table.

    Returns a compact dict: {table_name: row_count, ...} plus a 'total_claims'
    key so you can quickly gauge whether the pipeline has run.
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    tables = [
        "argument_claims",
        "claim_evidence",
        "claim_conflicts",
        "source_stances",
        "stance_drift_events",
        "policy_positions",
        "position_updates",
        "document_frames",
    ]
    counts: dict = {}
    for t in tables:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            counts[t] = row[0] if row else 0
        except Exception:
            counts[t] = "table_missing"

    counts["total_claims"] = counts.get("argument_claims", 0)
    return counts


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "claims": {"type": "array"}},
        "additionalProperties": True,
    },
)
def list_claims(
    source_type: Optional[str] = None,
    topic: Optional[str] = None,
    factchecked: Optional[bool] = None,
    limit: int = MAX_LIST,
) -> dict:
    """
    Compact list of extracted claims from ``argument_claims``.

    Args:
        source_type: Filter by content type (news/blog/paper/transcript/book/note).
        topic:       Substring match against the article's category (joined via
                     news_articles).
        factchecked: True = only claims with a factcheck verdict; False = only
                     unchecked claims; None = all.
        limit:       Max rows returned (default 30, max 100).

    Returns a list of dicts: {claim_id, source_type, confidence, factcheck_verdict,
    claim_preview, document_id}.
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    limit = min(limit, 100)
    where: list[str] = []
    params: list = []

    if source_type:
        where.append("c.source_type = ?")
        params.append(source_type)
    if factchecked is True:
        where.append("c.factcheck_verdict IS NOT NULL")
    elif factchecked is False:
        where.append("c.factcheck_verdict IS NULL")
    if topic:
        where.append("(n.category ILIKE ? OR n.title ILIKE ?)")
        params.extend([f"%{topic}%", f"%{topic}%"])

    join = "LEFT JOIN news_articles n ON c.document_id = n.article_id" if topic else ""
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    try:
        rows = conn.execute(
            f"""
            SELECT c.claim_id, c.source_type, c.confidence,
                   c.factcheck_verdict, c.claim_text, c.document_id
            FROM argument_claims c
            {join}
            {clause}
            ORDER BY c.confidence DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as e:
        return {"error": str(e)}

    return {
        "count": len(rows),
        "claims": [
            {
                "claim_id":         r[0],
                "source_type":      r[1],
                "confidence":       round(r[2], 3) if r[2] is not None else None,
                "factcheck_verdict": r[3],
                "claim_preview":    _clip(r[4]),
                "document_id":      r[5],
            }
            for r in rows
        ],
    }


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "stances": {"type": "array"}},
        "additionalProperties": True,
    },
)
def list_stances(
    source: Optional[str] = None,
    topic: Optional[str] = None,
    stance: Optional[str] = None,
    limit: int = MAX_LIST,
) -> dict:
    """
    Compact list of per-source stance aggregation rows from ``source_stances``.

    Args:
        source: Exact or partial source name match (ILIKE).
        topic:  Substring match against topic.
        stance: Filter by dominant stance (supportive/critical/neutral/ambiguous).
        limit:  Max rows (default 30, max 100).

    Returns a list of dicts: {source, source_type, topic, stance, confidence,
    document_count, window_start, window_end}.
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    limit = min(limit, 100)
    where: list[str] = []
    params: list = []

    if source:
        where.append("source ILIKE ?")
        params.append(f"%{source}%")
    if topic:
        where.append("topic ILIKE ?")
        params.append(f"%{topic}%")
    if stance:
        where.append("stance = ?")
        params.append(stance)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    try:
        rows = conn.execute(
            f"""
            SELECT source, source_type, topic, stance, confidence,
                   document_count, window_start, window_end
            FROM source_stances
            {clause}
            ORDER BY document_count DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as e:
        return {"error": str(e)}

    return {
        "count": len(rows),
        "stances": [
            {
                "source":         r[0],
                "source_type":    r[1],
                "topic":          r[2],
                "stance":         r[3],
                "confidence":     round(r[4], 3) if r[4] is not None else None,
                "document_count": r[5],
                "window_start":   r[6],
                "window_end":     r[7],
            }
            for r in rows
        ],
    }


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "events": {"type": "array"}},
        "additionalProperties": True,
    },
)
def list_drift_events(
    source: Optional[str] = None,
    topic: Optional[str] = None,
    limit: int = MAX_LIST,
) -> dict:
    """
    Stance-drift events from ``stance_drift_events``.

    Each event records a source switching from one dominant stance to another
    across two adjacent time windows on a given topic.

    Args:
        source: Exact or partial source name (ILIKE).
        topic:  Substring match against topic.
        limit:  Max rows (default 30, max 100).

    Returns a list of dicts: {source, source_type, topic, from_stance,
    to_stance, confidence_delta, window_pair, detected_at}.
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    limit = min(limit, 100)
    where: list[str] = []
    params: list = []

    if source:
        where.append("source ILIKE ?")
        params.append(f"%{source}%")
    if topic:
        where.append("topic ILIKE ?")
        params.append(f"%{topic}%")

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    try:
        rows = conn.execute(
            f"""
            SELECT source, source_type, topic, from_stance, to_stance,
                   confidence_delta, window_pair, detected_at
            FROM stance_drift_events
            {clause}
            ORDER BY detected_at DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as e:
        return {"error": str(e)}

    return {
        "count": len(rows),
        "events": [
            {
                "source":           r[0],
                "source_type":      r[1],
                "topic":            r[2],
                "from_stance":      r[3],
                "to_stance":        r[4],
                "confidence_delta": round(r[5], 3) if r[5] is not None else None,
                "window_pair":      r[6],
                "detected_at":      r[7],
            }
            for r in rows
        ],
    }


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "frames": {"type": "array"}},
        "additionalProperties": True,
    },
)
def list_frames(
    source_type: Optional[str] = None,
    frame: Optional[str] = None,
    limit: int = MAX_LIST,
) -> dict:
    """
    Frame distribution rows from ``document_frames``, aggregated per
    (frame, source_type): how often each framing appears and how confidently.

    Args:
        source_type: Filter by source type (news/blog/paper/...).
        frame:       Exact or partial frame label match (ILIKE).
        limit:       Max rows (default 30, max 100).

    Returns {"count": int, "frames": [{frame, source_type, doc_count,
    avg_score}, ...]}.
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    limit = min(limit, 100)
    where: list[str] = []
    params: list = []

    if source_type:
        where.append("source_type = ?")
        params.append(source_type)
    if frame:
        where.append("frame ILIKE ?")
        params.append(f"%{frame}%")

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    try:
        rows = conn.execute(
            f"""
            SELECT frame, source_type, COUNT(*) AS doc_count, AVG(score) AS avg_score
            FROM document_frames
            {clause}
            GROUP BY frame, source_type
            ORDER BY doc_count DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as e:
        return {"error": str(e)}

    return {
        "count": len(rows),
        "frames": [
            {
                "frame":       r[0],
                "source_type": r[1],
                "doc_count":   r[2],
                "avg_score":   float(r[3]) if r[3] is not None else 0.0,
            }
            for r in rows
        ],
    }


@mcp.tool
def claim_evidence_pairs(
    claim_id: Optional[str] = None,
    relation: Optional[str] = None,
    limit: int = MAX_LIST,
) -> dict:
    """
    Evidence pairs from ``claim_evidence`` — one claim referencing another.

    Unlike ``query_conflicts`` (pipeline_mcp) which reads the *semantic*
    conflict table, this reads the raw evidence-link table populated by the
    fact-check and cross-reference pipeline stages.

    Args:
        claim_id: Filter to pairs where this is the primary claim_id.
        relation: Filter by relation type (e.g. 'contradicts', 'supports',
                  'is_cited_by').
        limit:    Max rows (default 30, max 100).

    Returns a list of dicts: {evidence_id, claim_id, relation,
    evidence_source_type, similarity_score, evidence_preview, found_at}.
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    limit = min(limit, 100)
    where: list[str] = []
    params: list = []

    if claim_id:
        where.append("e.claim_id = ?")
        params.append(claim_id)
    if relation:
        where.append("e.relation = ?")
        params.append(relation)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    try:
        rows = conn.execute(
            f"""
            SELECT e.evidence_id, e.claim_id, e.relation,
                   e.evidence_source_type, e.similarity_score,
                   e.evidence_text, e.found_at
            FROM claim_evidence e
            {clause}
            ORDER BY e.similarity_score DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as e:
        return {"error": str(e)}

    return {
        "count": len(rows),
        "pairs": [
            {
                "evidence_id":         r[0],
                "claim_id":            r[1],
                "relation":            r[2],
                "evidence_source_type": r[3],
                "similarity_score":    round(r[4], 3) if r[4] is not None else None,
                "evidence_preview":    _clip(r[5] or ""),
                "found_at":            r[6],
            }
            for r in rows
        ],
    }


@mcp.tool
def list_unsourced_claims(
    source_type: Optional[str] = None,
    limit: int = MAX_LIST,
) -> dict:
    """
    Claims flagged as unsourced (``attributed = false``) from ``argument_claims``.

    Useful for auditing how many assertions in ingested content lack an
    attributed source.  Groups results by document_id so you can see the
    per-document unsourced count at a glance.

    Args:
        source_type: Filter by content type (news/blog/paper/transcript/book/note).
        limit:       Max rows (default 30, max 100).

    Returns a list of dicts: {claim_id, source_type, document_id, confidence,
    claim_preview} plus a top-level per_document_counts summary.
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    limit = min(limit, 100)
    where: list[str] = ["(attributed = false OR attributed IS NULL)"]
    params: list = []

    if source_type:
        where.append("source_type = ?")
        params.append(source_type)

    clause = "WHERE " + " AND ".join(where)
    params.append(limit)

    try:
        rows = conn.execute(
            f"""
            SELECT claim_id, source_type, document_id, confidence, claim_text
            FROM argument_claims
            {clause}
            ORDER BY confidence DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as e:
        return {"error": str(e)}

    per_doc: dict[str, int] = {}
    claims = []
    for r in rows:
        per_doc[r[2]] = per_doc.get(r[2], 0) + 1
        claims.append({
            "claim_id":      r[0],
            "source_type":   r[1],
            "document_id":   r[2],
            "confidence":    round(r[3], 3) if r[3] is not None else None,
            "claim_preview": _clip(r[4] or ""),
        })

    return {
        "count": len(claims),
        "per_document_counts": per_doc,
        "claims": claims,
    }


@mcp.tool
def trigger_attribution_batch(limit: int = 500) -> dict:
    """
    Run the attribution classifier over unclassified claims (``attributed IS NULL``).

    Calls ``run_attribution_batch`` from ``src.argument_mining.attribution``.
    Safe to call repeatedly — already-classified rows are skipped.

    Args:
        limit: Max claims to classify per call (default 500).

    Returns {"updated": int, "skipped": int}.
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    import threading
    try:
        from src.argument_mining.attribution import run_attribution_batch  # type: ignore[import]
    except ImportError as e:
        return {"error": f"attribution module unavailable: {e}"}

    lock = threading.Lock()
    # attribution batch needs write access; try to open rw
    import os, duckdb
    db_path = os.environ.get(
        "NEURONEWS_DB_PATH",
        str(REPO_ROOT / "data" / "local_warehouse.duckdb"),
    )
    try:
        rw_conn = duckdb.connect(db_path, read_only=False)
        result = run_attribution_batch(rw_conn, lock, limit=limit)
        rw_conn.close()
        return result
    except Exception as e:
        return {"error": f"write connection failed — warehouse may be locked: {e}"}


@mcp.tool
def list_actors(
    document_id: Optional[str] = None,
    source_type: Optional[str] = None,
    role: Optional[str] = None,
    actor_name: Optional[str] = None,
    limit: int = MAX_LIST,
) -> dict:
    """
    Query extracted actor/source metadata from ``document_actors``.

    Filter by any combination of document_id, source_type, role
    (speaker|subject|author), or partial actor_name.

    Returns compact rows with entity_id, role, and confidence.

    Args:
        document_id: Restrict to one document.
        source_type: news|blog|paper|transcript|book|note|web.
        role:        speaker, subject, or author.
        actor_name:  Substring match (case-insensitive).
        limit:       Max rows (default 50).
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    conditions: list[str] = []
    params: list = []

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
        conditions.append("lower(actor_name) LIKE ?")
        params.append(f"%{actor_name.lower()}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    try:
        rows = conn.execute(
            f"""
            SELECT document_id, source_type, actor_name, entity_id,
                   role, confidence, extracted_at
            FROM document_actors
            {where}
            ORDER BY confidence DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as e:
        return {"error": str(e)}

    return {
        "count": len(rows),
        "actors": [
            {
                "document_id":  r[0],
                "source_type":  r[1],
                "actor_name":   r[2],
                "entity_id":    r[3],
                "role":         r[4],
                "confidence":   round(r[5], 3) if r[5] is not None else None,
                "extracted_at": r[6],
            }
            for r in rows
        ],
    }


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "actors": {"type": "array"}},
        "additionalProperties": True,
    },
)
def actor_summary(
    source_type: Optional[str] = None,
    role: Optional[str] = None,
    limit: int = 30,
) -> dict:
    """
    Top actors by document frequency across all ingested content.

    Useful for seeding stance-detection and policy-position pipelines with a
    pre-ranked actor list without scanning full article text.

    Args:
        source_type: Filter by content type.
        role:        Filter by role (speaker|subject|author).
        limit:       Top-N (default 30).
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    conditions: list[str] = []
    params: list = []

    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)
    if role:
        conditions.append("role = ?")
        params.append(role)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    try:
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
    except Exception as e:
        return {"error": str(e)}

    return {
        "count": len(rows),
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
    }


@mcp.tool
def trigger_actor_batch(limit: int = 200) -> dict:
    """
    Extract actors for news_articles documents not yet in ``document_actors``.

    Calls ``run_actor_batch`` from ``src.argument_mining.metadata``.
    Safe to call repeatedly — already-processed documents are skipped.

    Args:
        limit: Max documents per call (default 200).

    Returns {"processed": int, "actors": int, "skipped": int}.
    """
    import os, threading, duckdb

    try:
        from src.argument_mining.metadata import run_actor_batch  # type: ignore[import]
    except ImportError as e:
        return {"error": f"metadata module unavailable: {e}"}

    db_path = os.environ.get(
        "NEURONEWS_DB_PATH",
        str(REPO_ROOT / "data" / "local_warehouse.duckdb"),
    )
    try:
        rw_conn = duckdb.connect(db_path, read_only=False)
        lock = threading.Lock()
        result = run_actor_batch(rw_conn, lock, limit=limit)
        rw_conn.close()
        return result
    except Exception as e:
        return {"error": f"write connection failed — warehouse may be locked: {e}"}


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "outlets": {"type": "array"}},
        "additionalProperties": True,
    },
)
def list_outlet_clusters(
    source_type: Optional[str] = None,
    cluster_id: Optional[int] = None,
    limit: int = MAX_LIST,
) -> dict:
    """
    Return stored outlet cluster assignments with PCA 2D coordinates.

    Each record includes source, cluster_id, descriptive cluster_label,
    pca_x/pca_y scatter coordinates, dominant_frame, and doc_count.

    Run ``trigger_outlet_clustering()`` first if the table is empty.

    Args:
        source_type: Filter by content type (news|blog|paper|transcript…).
        cluster_id:  Filter to one cluster.
        limit:       Max rows (default 50).
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    conditions: list[str] = []
    params: list = []
    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)
    if cluster_id is not None:
        conditions.append("cluster_id = ?")
        params.append(cluster_id)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    try:
        rows = conn.execute(
            f"""
            SELECT source, source_type, cluster_id, cluster_label,
                   ROUND(pca_x, 3) AS pca_x, ROUND(pca_y, 3) AS pca_y,
                   dominant_frame, doc_count, computed_at
            FROM outlet_clusters
            {where}
            ORDER BY cluster_id, doc_count DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as e:
        return {"error": str(e)}

    return {
        "count": len(rows),
        "outlets": [
            {
                "source":         r[0],
                "source_type":    r[1],
                "cluster_id":     r[2],
                "cluster_label":  r[3],
                "pca_x":          r[4],
                "pca_y":          r[5],
                "dominant_frame": r[6],
                "doc_count":      r[7],
                "computed_at":    r[8],
            }
            for r in rows
        ],
    }


@mcp.tool
def trigger_outlet_clustering(
    date_range: str = "90d",
    k_min: int = 2,
    k_max: int = 8,
) -> dict:
    """
    Embed news outlets as 7-dim frame vectors, run k-means + hierarchical
    clustering (k_min..k_max), pick best k by silhouette score, project to
    2D via PCA, and persist results to ``outlet_clusters``.

    Safe to call repeatedly — each call overwrites the previous run.

    Args:
        date_range: Frame aggregation window: 7d|30d|90d|180d|365d (default "90d").
        k_min:      Minimum k to try (default 2).
        k_max:      Maximum k to try (default 8).

    Returns {"n_outlets", "k", "method", "silhouette", "computed_at"}.
    """
    import os, threading, duckdb

    try:
        from src.argument_mining.outlet_clustering import run_cluster_pipeline  # type: ignore[import]
    except ImportError as e:
        return {"error": f"outlet_clustering module unavailable: {e}"}

    db_path = os.environ.get(
        "NEURONEWS_DB_PATH",
        str(REPO_ROOT / "data" / "local_warehouse.duckdb"),
    )
    try:
        rw_conn = duckdb.connect(db_path, read_only=False)
        lock = threading.Lock()
        result = run_cluster_pipeline(
            rw_conn, lock, date_range=date_range, k_min=k_min, k_max=k_max
        )
        rw_conn.close()
        return result
    except Exception as e:
        return {"error": f"write connection failed — warehouse may be locked: {e}"}


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "outlets": {"type": "array"}},
        "additionalProperties": True,
    },
)
def list_outlet_scores(
    source_type: Optional[str] = None,
    sort_by: str = "composite_score",
    limit: int = 30,
) -> dict:
    """
    Return latest weekly transparency scores for all outlets.

    Each record includes frame_diversity, attribution_rate, stance_neutrality,
    composite_score, doc_count, claim_count, and score_date.

    Args:
        source_type: Filter by content type (news|blog|paper|transcript…).
        sort_by:     composite_score|frame_diversity|attribution_rate|stance_neutrality.
        limit:       Max rows (default 30).
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}

    _SORT_COLS = {"composite_score", "frame_diversity", "attribution_rate", "stance_neutrality"}
    sort_col = sort_by if sort_by in _SORT_COLS else "composite_score"

    conditions: list[str] = []
    params: list = []
    if source_type:
        conditions.append("source_type = ?")
        params.append(source_type)

    where_extra = f"AND {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    try:
        rows = conn.execute(
            f"""
            SELECT source, source_type, score_date,
                   frame_diversity, attribution_rate, stance_neutrality,
                   composite_score, doc_count, claim_count, computed_at
            FROM outlet_scores os
            WHERE (source, source_type, score_date) IN (
                SELECT source, source_type, MAX(score_date)
                FROM outlet_scores
                WHERE 1=1 {where_extra}
                GROUP BY source, source_type
            )
            ORDER BY {sort_col} DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
    except Exception as e:
        return {"error": str(e)}

    return {
        "count": len(rows),
        "outlets": [
            {
                "source":            r[0],
                "source_type":       r[1],
                "score_date":        r[2],
                "frame_diversity":   round(r[3], 4) if r[3] is not None else None,
                "attribution_rate":  round(r[4], 4) if r[4] is not None else None,
                "stance_neutrality": round(r[5], 4) if r[5] is not None else None,
                "composite_score":   round(r[6], 4) if r[6] is not None else None,
                "doc_count":         r[7],
                "claim_count":       r[8],
                "computed_at":       r[9],
            }
            for r in rows
        ],
    }


@mcp.tool
def trigger_outlet_scoring(date_range: str = "90d") -> dict:
    """
    Compute weekly transparency scores for all outlets and persist to ``outlet_scores``.

    Idempotent — re-running in the same ISO week overwrites that week's row.

    Scores three dimensions per outlet:
      - frame_diversity:   Shannon entropy of 7-frame distribution / log(7)
      - attribution_rate:  % claims with attributed=True
      - stance_neutrality: entropy of stance distribution / log(4)
      - composite_score:   mean of the three

    Args:
        date_range: Data window: 7d|14d|30d|90d|180d|365d (default "90d").

    Returns {"outlets_scored", "score_date", "top_outlet", "top_composite"}.
    """
    import os, threading, duckdb

    try:
        from src.argument_mining.outlet_scorer import run_scorer_batch  # type: ignore[import]
    except ImportError as e:
        return {"error": f"outlet_scorer module unavailable: {e}"}

    db_path = os.environ.get(
        "NEURONEWS_DB_PATH",
        str(REPO_ROOT / "data" / "local_warehouse.duckdb"),
    )
    try:
        rw_conn = duckdb.connect(db_path, read_only=False)
        lock = threading.Lock()
        result = run_scorer_batch(rw_conn, lock, date_range=date_range)
        rw_conn.close()
        return result
    except Exception as e:
        return {"error": f"write connection failed — warehouse may be locked: {e}"}


@mcp.tool
def get_benchmark_results(model: Optional[str] = None) -> dict:
    """
    Read the latest argument-mining benchmark results from ``docs/benchmark_results.json``.

    Returns the stored JSON without running any inference — instant and read-only.
    Run ``python scripts/benchmark_models.py`` to refresh the results first.

    Args:
        model: Filter to a single model: "claim_detector" | "stance_classifier" |
               "frame_classifier".  Omit to return all three plus IAA and cross-dataset.

    Returns dict with keys: evaluated_at, claim_detector, stance_classifier,
    frame_classifier, iaa, cross_dataset (or the single requested model's sub-dict).
    """
    import json as _json

    bench_path = REPO_ROOT / "docs" / "benchmark_results.json"
    if not bench_path.exists():
        return {
            "error": "docs/benchmark_results.json not found",
            "hint": "Run: python scripts/benchmark_models.py",
        }
    try:
        data = _json.loads(bench_path.read_text())
    except Exception as e:
        return {"error": f"could not parse benchmark_results.json: {e}"}

    if model:
        if model not in data:
            return {
                "error": f"unknown model '{model}'",
                "available": [k for k in data if k != "evaluated_at"],
            }
        return {
            "evaluated_at": data.get("evaluated_at"),
            model: data[model],
        }

    # Return compact summary to avoid bloating context
    def _compact(m: dict) -> dict:
        o = m.get("overall", {})
        return {
            "mode":          o.get("mode"),
            "f1":            o.get("f1") or o.get("macro_f1"),
            "accuracy":      o.get("accuracy"),
            "n":             o.get("n"),
            "per_source_type": {
                stype: {"f1": v.get("f1") or v.get("macro_f1"), "n": v.get("n")}
                for stype, v in m.get("per_source_type", {}).items()
            },
        }

    return {
        "evaluated_at":      data.get("evaluated_at"),
        "claim_detector":    _compact(data.get("claim_detector", {})),
        "stance_classifier": _compact(data.get("stance_classifier", {})),
        "frame_classifier":  _compact(data.get("frame_classifier", {})),
        "iaa":               data.get("iaa", {}),
        "cross_dataset":     {k: {"f1": v.get("f1"), "n": v.get("n")}
                              for k, v in data.get("cross_dataset", {}).items()},
    }


# --------------------------------------------------------------------------- #
# Analytics plane (R5 / Track DS): confidence + significance for the           #
# transparency figures, under the statistical-honesty contract. These feed     #
# error bars / significance badges on the ranking + stance panels; they are    #
# not panels themselves, so they carry no meta.panel annotation.               #
# --------------------------------------------------------------------------- #

@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "outlet": {"type": "string"},
            "composite": INTERVAL_SCHEMA,
            "components": {"type": "object"},
            "note": {"type": "string"},
        }
    ),
)
def score_confidence(outlet: str) -> dict:
    """Bootstrap confidence interval on an outlet's composite transparency
    score, from its weekly ``outlet_scores`` history, so the ranking panel can
    draw an error bar instead of a bare number.

    Args:
        outlet: the source name (as stored in ``outlet_scores.source``).
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}
    try:
        from src.analytics.confidence import score_confidence_payload

        return score_confidence_payload(conn, outlet)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "topic": {"type": "string"},
            "outlet_a": {"type": "string"},
            "outlet_b": {"type": "string"},
            "chi_square": {"type": "number"},
            "p_value": {"type": "number"},
            "significant": {"type": "boolean"},
            "divergence": INTERVAL_SCHEMA,
            "categories": {"type": "array"},
            "note": {"type": "string"},
        }
    ),
)
def stance_significance(a: str, b: str, topic: str) -> dict:
    """Permutation test on whether two outlets' stance splits on a topic
    genuinely differ, with a bootstrap interval on the divergence effect size,
    so a stance comparison can carry a significance badge.

    Args:
        a:     first outlet (source name).
        b:     second outlet.
        topic: the contested topic to compare on.
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}
    try:
        from src.analytics.confidence import stance_significance_payload

        return stance_significance_payload(conn, a, b, topic)
    except Exception as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------- #
# Relation extraction (document_relations): subject-relation-object triples
# between entities. Entities come from the same NER as the actor batch; the
# batch is built by trigger_relation_extraction.
# --------------------------------------------------------------------------- #


@mcp.tool
def document_relations(document_id: str) -> dict:
    """Relations extracted from one document (subject-relation-object), each
    cited to its source sentence.

    Args:
        document_id: the document to list relations for.
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}
    try:
        from src.argument_mining.relations import document_relations as _dr

        return _dr(conn, document_id)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool
def entity_relations(entity: str) -> dict:
    """Relations involving an entity, as subject or object (by name or entity_id).

    Args:
        entity: the entity name or ``ent-...`` id.
    """
    conn, err = _warehouse_ro()
    if err or conn is None:
        return {"error": err or "no connection"}
    try:
        from src.argument_mining.relations import entity_relations as _er

        return _er(conn, entity)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool
def trigger_relation_extraction(limit: int = 200) -> dict:
    """Extract relations for corpus documents not yet processed into
    ``document_relations`` (RW). Safe to call repeatedly.

    Args:
        limit: max documents per call (default 200).

    Returns {"documents_processed": int, "relations_found": int}.
    """
    import os
    import duckdb

    try:
        from src.argument_mining.relations import extract_document_relations
    except ImportError as e:
        return {"error": f"relations module unavailable: {e}"}

    db_path = os.environ.get(
        "NEURONEWS_DB_PATH", str(REPO_ROOT / "data" / "local_warehouse.duckdb")
    )
    try:
        rw_conn = duckdb.connect(db_path, read_only=False)
        result = extract_document_relations(rw_conn, limit=limit)
        rw_conn.close()
        return result
    except Exception as e:
        return {"error": f"write connection failed — warehouse may be locked: {e}"}


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http
