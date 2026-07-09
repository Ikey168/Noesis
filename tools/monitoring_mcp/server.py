"""
NeuroNews resource monitor — MCP server.

Token-efficient tools for inspecting local system metrics collected by
the background resource monitor without reading DuckDB directly or running
shell commands.

Tools:

  current_metrics()            -> live psutil reading (no DB)
  get_metrics(metric?, n?)     -> recent samples from DuckDB
  get_summary()                -> 1h / 24h averages per metric
  trigger_sample()             -> collect + store one sample right now
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

mcp = FastMCP("neuronews-monitoring")


def _conn():
    from src.database.local_analytics_connector import get_shared_connection
    return get_shared_connection()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def current_metrics() -> dict:
    """
    Return a live psutil reading — does NOT touch the database.

    Use this for a quick health check without caring about history.
    Returns a dict mapping metric_name → {value, unit}.
    """
    from src.monitoring.resource_monitor import collect_sample
    rows = collect_sample()
    return {r["metric_name"]: {"value": r["value"], "unit": r["unit"]} for r in rows}


@mcp.tool()
def get_metrics(
    metric_name: Optional[str] = None,
    n: int = 50,
    since_minutes: Optional[int] = None,
) -> list:
    """
    Return recent resource metric samples from DuckDB.

    Args:
        metric_name:    Optional filter (e.g. ``"cpu_percent"``).
        n:              Max rows to return (default 50).
        since_minutes:  Only return rows from the last N minutes.

    Returns a list of dicts: sampled_at, metric_name, value, unit, pid,
    process_name. Empty list if no data yet (collector starts after startup).
    """
    from src.monitoring.resource_monitor import get_metrics as _get
    return _get(_conn(), metric_name=metric_name, n=n, since_minutes=since_minutes)


@mcp.tool()
def get_summary() -> dict:
    """
    Return 1-hour and 24-hour averages, min, and max for each metric.

    Returns::

        {
          "1h":  {"cpu_percent": {"avg": 12.3, "min": 5.1, "max": 40.2, "unit": "%"}, ...},
          "24h": {...},
          "sampled_at": "<iso timestamp>"
        }

    Windows with no data return ``{}``.
    """
    from src.monitoring.resource_monitor import get_summary as _gs
    return _gs(_conn())


@mcp.tool()
def trigger_sample() -> dict:
    """
    Collect one metric sample right now and persist it to DuckDB.

    Returns the six metric rows that were collected.
    Useful when you want an immediate reading outside the 60-second collection
    cycle — e.g. before and after running a heavy pipeline stage.
    """
    from src.monitoring.resource_monitor import collect_and_store
    rows = collect_and_store(_conn())
    return {
        "stored": len(rows),
        "metrics": {r["metric_name"]: {"value": r["value"], "unit": r["unit"]} for r in rows},
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http
