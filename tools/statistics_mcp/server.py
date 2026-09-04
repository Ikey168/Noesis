"""
Noesis Statistics inspector — MCP server (Track A / A2).

Panel-facing read tools over the dataset observation store: official
statistical series (World Bank, FRED, Eurostat, ...) harvested as evidence for
checking quantitative claims. Annotated for R2 discovery so the
``series_explorer`` panel surfaces automatically once a harvest has populated
the store.

Named ``noesis-statistics`` to stay distinct from ``noesis-dataset`` (the
argument-mining dataset inspector, a different thing entirely).

Design constraints (same as the other tool servers):
  * Lazy imports inside tools; the top of this module imports only stdlib +
    fastmcp so the server starts fast.
  * The DuckDB warehouse is opened READ-ONLY.
  * Reads are summaries capped by the query module; the full observation table
    is never returned in one call.

See docs/architecture/EVIDENCE_DATASETS_PLAN.md.
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

mcp = FastMCP("noesis-statistics")

_SERIES_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "series_id": {"type": "string"},
        "provider": {"type": "string"},
        "title": {"type": "string"},
        "unit": {"type": ["string", "null"]},
        "frequency": {"type": "string"},
        "geography": {"type": ["string", "null"]},
        "license": {"type": ["string", "null"]},
        "as_of": {"type": ["integer", "null"]},
    },
    "additionalProperties": True,
}


def _warehouse_ro():
    """Open the DuckDB warehouse read-only, honouring NOESIS_DB_PATH."""
    import duckdb

    from src.config.env import warehouse_path

    path = warehouse_path(str(REPO_ROOT / "data" / "neuronews.duckdb"))
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"warehouse not found at {path}")
    return duckdb.connect(path, read_only=True)


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "series": {"type": "array", "items": _SERIES_ITEM_SCHEMA},
            "count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
        "additionalProperties": True,
    },
)
def list_series(
    provider: Optional[str] = None,
    geography: Optional[str] = None,
    query: Optional[str] = None,
) -> dict:
    """List harvested statistical series, optionally filtered.

    Args:
        provider: restrict to one provider (e.g. 'worldbank').
        geography: restrict to an ISO 3166 alpha-2 / region code (e.g. 'DE').
        query: case-insensitive substring of the title or series_id.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:  # noqa: BLE001
        return {"series": [], "count": 0, "truncated": False, "note": str(exc)}
    try:
        from src.ingestion.connectors.dataset.queries import list_series as _ls

        return _ls(con, provider=provider, geography=geography, query=query)
    finally:
        con.close()


@mcp.tool(
    output_schema={"type": "object", "additionalProperties": True},
)
def get_series(series_id: str) -> dict:
    """Return one series header plus a latest-vintage summary.

    Args:
        series_id: provider-scoped id, e.g. 'wb:SL.UEM.TOTL.ZS:DE'.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    try:
        from src.ingestion.connectors.dataset.queries import get_series as _gs

        return _gs(con, series_id)
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "series_id": {"type": "string"},
            "as_of": {"type": ["integer", "null"]},
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "period": {"type": "string"},
                        "value": {"type": ["number", "null"]},
                    },
                },
            },
            "truncated": {"type": "boolean"},
        },
        "additionalProperties": True,
    },
)
def get_observations(series_id: str, as_of: Optional[int] = None) -> dict:
    """Return a series' observations at a vintage (latest when omitted), capped.

    Args:
        series_id: provider-scoped id.
        as_of: vintage (ms since epoch); omit for the latest revision.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:  # noqa: BLE001
        return {"series_id": series_id, "observations": [], "truncated": False, "note": str(exc)}
    try:
        from src.ingestion.connectors.dataset.queries import get_observations as _go

        return _go(con, series_id, as_of=as_of)
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "series": {"type": "array", "items": _SERIES_ITEM_SCHEMA},
            "count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
        "additionalProperties": True,
    },
)
def series_explorer(topic: Optional[str] = None) -> dict:
    """Panel payload: series matching an optional topic with spark-line summaries.

    Args:
        topic: optional case-insensitive substring filter.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:  # noqa: BLE001
        return {"series": [], "count": 0, "truncated": False, "note": str(exc)}
    try:
        from src.ingestion.connectors.dataset.queries import series_explorer as _se

        return _se(con, topic)
    finally:
        con.close()


_CHECK_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "check_id": {"type": "string"},
        "subject": {"type": ["string", "null"]},
        "direction": {"type": ["string", "null"]},
        "series_id": {"type": ["string", "null"]},
        "verdict": {"type": "string"},
        "match_confidence": {"type": ["number", "null"]},
    },
    "additionalProperties": True,
}


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "checks": {"type": "array", "items": _CHECK_ITEM_SCHEMA},
            "count": {"type": "integer"},
        },
        "additionalProperties": True,
    },
)
def claim_vs_data(topic: Optional[str] = None) -> dict:
    """Recent claim-vs-data checks with verdicts and observed intervals.

    Args:
        topic: optional case-insensitive substring of the claim subject.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:  # noqa: BLE001
        return {"checks": [], "count": 0, "note": str(exc)}
    try:
        from src.analytics.claim_check import claim_vs_data as _cvd

        return _cvd(con, topic)
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "checks": {"type": "array", "items": _CHECK_ITEM_SCHEMA},
            "count": {"type": "integer"},
            "verdict": {"type": ["string", "null"]},
        },
        "additionalProperties": True,
    },
)
def data_check_ledger(verdict: Optional[str] = "contradicted", topic: Optional[str] = None) -> dict:
    """Recorded checks, defaulting to the contradicted ones (the ledger).

    Args:
        verdict: filter by verdict; defaults to 'contradicted'. Pass null for all.
        topic: optional case-insensitive substring of the claim subject.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:  # noqa: BLE001
        return {"checks": [], "count": 0, "note": str(exc)}
    try:
        from src.analytics.claim_check import data_check_ledger as _dcl

        return _dcl(con, verdict=verdict, topic=topic)
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "series_count": {"type": "integer"},
            "observation_count": {"type": "integer"},
            "providers": {"type": "array", "items": {"type": "string"}},
            "available": {"type": "boolean"},
        },
        "additionalProperties": True,
    },
)
def stats() -> dict:
    """Aggregate counts for R3 availability resolution (series/observations/providers)."""
    try:
        con = _warehouse_ro()
    except Exception as exc:  # noqa: BLE001
        return {"series_count": 0, "observation_count": 0, "providers": [], "available": False, "note": str(exc)}
    try:
        from src.ingestion.connectors.dataset.queries import available

        if not available(con):
            return {"series_count": 0, "observation_count": 0, "providers": [], "available": False}
        series_count = con.execute("SELECT COUNT(*) FROM dataset_series").fetchone()[0]
        obs_count = con.execute("SELECT COUNT(*) FROM dataset_observations").fetchone()[0]
        providers = [r[0] for r in con.execute("SELECT DISTINCT provider FROM dataset_series ORDER BY provider").fetchall()]
        return {
            "series_count": series_count,
            "observation_count": obs_count,
            "providers": providers,
            "available": True,
        }
    finally:
        con.close()


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http
