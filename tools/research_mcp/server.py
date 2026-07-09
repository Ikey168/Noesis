"""
NeuroNews Research-domain inspector — MCP server (R7 / Track N1).

Panel-facing read tools for the research pack: venue credibility (transparency
scoring generalized to publication venues), the paper citation graph, and
literature claims (SUPPORTS/CONTRADICTS scoped to papers). Annotated for R2
discovery so the `venues`, `citation_graph` and `literature_claims` panels
surface automatically when the research pack is enabled.

Design constraints (same as the other tool servers):
  * Lazy imports inside tools; the top of this module imports only stdlib +
    fastmcp (plus the stdlib-only honesty helper) so the server starts fast.
  * The DuckDB warehouse is opened READ-ONLY.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analytics.honesty import INTERVAL_SCHEMA, honesty_output_schema  # noqa: E402

mcp = FastMCP("neuronews-research")


def _warehouse_ro():
    """Open the DuckDB warehouse read-only, honouring NEURONEWS_DB_PATH."""
    import duckdb

    from src.config.env import warehouse_path
    path = warehouse_path(str(REPO_ROOT / "data" / "neuronews.duckdb"))
    if not os.path.exists(path):
        raise FileNotFoundError(f"warehouse not found at {path}")
    return duckdb.connect(path, read_only=True)


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "venue_count": {"type": "integer"},
            "venues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "venue": {"type": "string"},
                        "papers": {"type": "integer"},
                        "credibility": INTERVAL_SCHEMA,
                        "components": {"type": "object"},
                    },
                },
            },
        }
    ),
)
def venues() -> dict:
    """Per-venue credibility over the ingested paper corpus, reusing the
    transparency-scoring machinery. Each venue carries a credibility interval
    and its component scores.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.domains.research.analytics import venue_credibility

        return venue_credibility(con)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "nodes": {"type": "array"},
            "edges": {"type": "array"},
            "node_count": {"type": "integer"},
            "edge_count": {"type": "integer"},
        },
        "additionalProperties": True,
    },
)
def citation_graph(topic: Optional[str] = None) -> dict:
    """The paper citation network (nodes = papers, edges = citations) from the
    document corpus, optionally scoped to a topic/concept.

    Args:
        topic: optional concept or title filter.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.domains.research.analytics import citation_graph as _cg

        return _cg(con, topic)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "claims": {"type": "array"},
            "count": {"type": "integer"},
            "topic": {"type": ["string", "null"]},
        },
        "additionalProperties": True,
    },
)
def literature_claims(topic: Optional[str] = None) -> dict:
    """SUPPORTS/CONTRADICTS-style claims scoped to papers, from the claim layer.

    Args:
        topic: optional substring filter on the claim text.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.domains.research.analytics import literature_claims as _lc

        return _lc(con, topic)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


if __name__ == "__main__":
    mcp.run()
