"""
NeuroNews multi-source comparison inspector — MCP server.

Token-efficient read-only access to the source comparison engine and the
outlet trust / cluster data stored in the analytics warehouse.

Tools:

  compare_sources(topic, limit?, days?)     -> per-source comparison
  list_sources(days?, source_type?)         -> unique sources with article counts
  get_source_profile(source)                -> trust scores + cluster + stances
  list_trustworthiness(source_type?,        -> all scored sources with bias metrics
                       date_range?)
  outlet_clusters(source_type?)             -> editorial cluster assignments

Design constraints (same as other NeuroNews MCP servers):
  * Lazy imports inside each tool — top-level imports are stdlib + fastmcp only.
  * Results are capped summaries, never full payloads.
  * Read-only — no tool mutates the warehouse.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

mcp = FastMCP("neuronews-sources")

MAX_LIST = 50


def _get_conn():
    from src.database.local_analytics_connector import get_shared_connection
    return get_shared_connection()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def compare_sources(
    topic: str,
    limit: int = 10,
    days: int = 90,
) -> dict:
    """
    Compare how different media outlets cover *topic*.

    Args:
        topic:  Keyword matched against article title and content.
        limit:  Max sources to return (default 10, max 50).
        days:   Look-back window in days (default 90).

    Returns per-source: article_count, coverage_share_pct, avg_sentiment,
    dominant_sentiment, sentiment_distribution, trust_scores (when scored),
    cluster (when clustered), stance (when available). Also a cross-source
    summary (most_positive_source, most_negative_source, most_trusted_source,
    highest_coverage_source, stance_spread).
    """
    from src.nlp.source_comparator import compare_sources as _compare
    return _compare(topic=topic, conn=_get_conn(), limit=min(limit, MAX_LIST), days=days)


@mcp.tool()
def list_sources(
    days: int = 90,
    source_type: Optional[str] = None,
) -> list:
    """
    List unique source names in the analytics warehouse with article counts
    and average sentiment.

    Args:
        days:         Look-back window in days (default 90).
        source_type:  Optional — not stored in news_articles; used to filter
                      the outlet_scores join if provided.

    Returns list of {source, article_count, avg_sentiment, category}.
    """
    conn = _get_conn()
    sql = f"""
        SELECT
            source,
            COUNT(*)                AS article_count,
            AVG(sentiment_score)    AS avg_sentiment,
            MAX(category)           AS category
        FROM news_articles
        WHERE publish_date >= CURRENT_TIMESTAMP - INTERVAL '{days} days'
        GROUP BY source
        ORDER BY article_count DESC
        LIMIT {MAX_LIST}
    """
    rows = conn.execute(sql).fetchall()
    return [
        {
            "source": r[0],
            "article_count": r[1],
            "avg_sentiment": round(float(r[2]), 4) if r[2] is not None else None,
            "category": r[3],
        }
        for r in rows
    ]


@mcp.tool()
def get_source_profile(source: str) -> dict:
    """
    Return a full profile for one named source: article volume, average
    sentiment, trust scores, editorial cluster, and per-topic stances.

    Args:
        source:  Exact source name (e.g. "BBC World", "NYT Technology").
                 Use list_sources() to see available names.
    """
    from src.nlp.source_comparator import get_source_profile as _profile
    return _profile(source=source, conn=_get_conn())


@mcp.tool()
def list_trustworthiness(
    source_type: Optional[str] = None,
    date_range: str = "30d",
) -> list:
    """
    List all sources with their latest outlet trust scores.

    Scores come from the outlet-scoring pipeline. Returns an empty list when
    the scoring job has not yet been run.

    Args:
        source_type:  Optional filter (e.g. "news", "blog").
        date_range:   "30d" | "90d" | "all"  (default "30d").

    Returns list of {source, source_type, frame_diversity, attribution_rate,
    stance_neutrality, composite_score, doc_count, score_date}.
    """
    from src.nlp.source_comparator import list_source_trustworthiness
    return list_source_trustworthiness(
        conn=_get_conn(),
        source_type=source_type,
        date_range=date_range,
    )


@mcp.tool()
def outlet_clusters(source_type: Optional[str] = None) -> list:
    """
    Return editorial cluster assignments for all outlets that have been
    clustered.

    Args:
        source_type:  Optional filter (e.g. "news").

    Returns list of {source, cluster_id, cluster_label, dominant_frame,
    pca_x, pca_y, doc_count}.
    """
    conn = _get_conn()
    where = "WHERE source_type = ?" if source_type else ""
    params = [source_type] if source_type else []
    sql = f"""
        SELECT source, cluster_id, cluster_label, dominant_frame,
               pca_x, pca_y, doc_count
        FROM outlet_clusters
        {where}
        ORDER BY cluster_id, source
        LIMIT {MAX_LIST}
    """
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "source": r[0],
            "cluster_id": r[1],
            "cluster_label": r[2],
            "dominant_frame": r[3],
            "pca_x": round(float(r[4]), 4) if r[4] is not None else None,
            "pca_y": round(float(r[5]), 4) if r[5] is not None else None,
            "doc_count": r[6],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http
