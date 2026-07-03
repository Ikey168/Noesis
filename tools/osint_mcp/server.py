"""
NeuroNews OSINT composition - MCP server (R10 / Track OSINT phase 1).

Defensive, analytical primitives over already-ingested public documents, each a
pure composition of layers Noesis already builds. Nothing here crawls, targets
or de-anonymizes; the tools only read the warehouse.

Tools (all annotated for R2 discovery under the `osint` ui_flag):
  corroborate(claim_id)              -> corroboration panel: independent sources
                                        for/against, weighted by credibility
  source_reliability(source)         -> reliability card: transparency,
                                        corroboration hit-rate, corrections
  contradiction_scan(topic?, entity?)-> contradiction ledger: cited CONTRADICTS
                                        pairs, uncited flagged not hidden

Design constraints (as for every tool server): stdlib + fastmcp (plus the
stdlib-only honesty helper) at import time, lazy imports inside tools, the
warehouse opened READ-ONLY.
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

mcp = FastMCP("neuronews-osint")


def _warehouse_ro():
    import duckdb

    path = os.getenv("NEURONEWS_DB_PATH", str(REPO_ROOT / "data" / "neuronews.duckdb"))
    if not os.path.exists(path):
        raise FileNotFoundError(f"warehouse not found at {path}")
    return duckdb.connect(path, read_only=True)


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "claim": {"type": "object"},
            "support": {"type": "array"},
            "contradict": {"type": "array"},
            "independent_support_count": {"type": "integer"},
            "independent_contradict_count": {"type": "integer"},
            "weighted_support": {"type": "number"},
            "weighted_contradict": {"type": "number"},
            "single_sourced": {"type": "boolean"},
        }
    ),
    meta={"panel": {
        "type": "corroboration",
        "title": "Claim corroboration",
        "description": "Independent sources supporting or contradicting a claim, weighted by source credibility; single-sourced claims are flagged, never given a false confidence.",
        "endpoint": None,
        "facets": ["claims", "sources", "conflict"],
        "tables": ["argument_claims"],
        "ui_flag": "osint",
        "default_span": 6,
    }},
)
def corroborate(claim_id: str) -> dict:
    """How many independent sources support or contradict a claim, and how
    credible they are. Never collapses to a single confidence number.

    Args:
        claim_id: the claim to corroborate (see argument_mcp.list_claims).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import corroborate as _corroborate

        return _corroborate(con, claim_id)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "source": {"type": "string"},
            "found": {"type": "boolean"},
            "reliability": INTERVAL_SCHEMA,
            "components": {"type": "object"},
            "track_record": {"type": "object"},
            "corroboration": {"type": "object"},
            "corrections": {"type": "object"},
            "scored_as_outlet": {"type": "boolean"},
        }
    ),
    meta={"panel": {
        "type": "reliability_card",
        "title": "Source reliability",
        "description": "OSINT source vetting: the outlet transparency score generalized to any source type, with corroboration hit-rate and correction history.",
        "endpoint": None,
        "facets": ["sources"],
        "tables": ["outlet_scores"],
        "ui_flag": "osint",
        "default_span": 6,
    }},
)
def source_reliability(source: str) -> dict:
    """Reliability card for any source (blog, paper venue, filing, outlet),
    scored the same way outlets are.

    Args:
        source: the source name to vet (see sources_mcp.list_sources).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import source_reliability as _reliability

        return _reliability(con, source)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "contradictions": {"type": "array"},
            "count": {"type": "integer"},
            "uncited_count": {"type": "integer"},
            "topic": {"type": ["string", "null"]},
            "entity": {"type": ["string", "null"]},
        },
        "additionalProperties": True,
    },
    meta={"panel": {
        "type": "contradiction_ledger",
        "title": "Contradiction ledger",
        "description": "Where the public record disagrees with itself: contradicting claim pairs with both sources and citations; uncited entries are flagged, never hidden.",
        "endpoint": None,
        "facets": ["conflict", "claims"],
        "tables": ["claim_conflicts"],
        "ui_flag": "osint",
        "default_span": 6,
        "topic_param": "topic",
    }},
)
def contradiction_scan(
    topic: Optional[str] = None, entity: Optional[str] = None
) -> dict:
    """Contradiction pairs on a topic or entity, each cited back to its source
    document. Uncited entries are flagged, not dropped.

    Args:
        topic: optional topic filter (conflict topic or claim-text substring).
        entity: optional entity filter (substring of either claim's text).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.osint import contradiction_scan as _scan

        return _scan(con, topic=topic, entity=entity)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


if __name__ == "__main__":
    mcp.run()
