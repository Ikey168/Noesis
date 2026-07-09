"""
Panel-facing reads over the dataset observation store (A2).

Pure functions over a DuckDB connection so they are unit-testable without a
running MCP server: write series with :class:`ObservationStore`, read them here.
Every function is defensive — a warehouse with no dataset tables (no harvest has
run yet) degrades to an empty but valid payload rather than raising, so the
``series_explorer`` panel shows an empty state instead of an error.

All reads are summaries capped at module limits; the full observation table is
never returned in one call (shared MCP-server discipline).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

MAX_SERIES = 100  # list_series / series_explorer cap
MAX_OBS = 1000  # get_observations cap


def _table_exists(conn, table: str) -> bool:
    try:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall()
        return bool(rows)
    except Exception:  # noqa: BLE001 - a missing catalog is an empty warehouse
        return False


def available(conn) -> bool:
    """True when the dataset tables exist (a harvest has populated the store)."""
    return _table_exists(conn, "dataset_series") and _table_exists(conn, "dataset_observations")


def list_series(
    conn,
    provider: Optional[str] = None,
    geography: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = MAX_SERIES,
) -> Dict[str, Any]:
    """List series headers, optionally filtered by provider, geography, or a
    case-insensitive substring of the title/series_id."""
    if not available(conn):
        return {"series": [], "count": 0, "truncated": False, "note": "no dataset series harvested"}
    clauses: List[str] = []
    params: List[Any] = []
    if provider is not None:
        clauses.append("provider = ?")
        params.append(provider)
    if geography is not None:
        clauses.append("geography = ?")
        params.append(geography)
    if query:
        clauses.append("(LOWER(title) LIKE ? OR LOWER(series_id) LIKE ?)")
        needle = f"%{query.lower()}%"
        params.extend([needle, needle])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    capped = max(1, min(limit, MAX_SERIES))
    rows = conn.execute(
        f"""
        SELECT series_id, provider, title, unit, frequency, geography, license, as_of
        FROM dataset_series{where}
        ORDER BY series_id
        LIMIT {capped + 1}
        """,
        params,
    ).fetchall()
    keys = ["series_id", "provider", "title", "unit", "frequency", "geography", "license", "as_of"]
    truncated = len(rows) > capped
    series = [dict(zip(keys, r)) for r in rows[:capped]]
    return {"series": series, "count": len(series), "truncated": truncated}


def get_series(conn, series_id: str) -> Dict[str, Any]:
    """Return one series header plus a small latest-vintage summary."""
    if not available(conn):
        return {"error": "no dataset series harvested"}
    row = conn.execute(
        """
        SELECT series_id, provider, title, unit, frequency, geography, license, as_of, source_url, metadata
        FROM dataset_series WHERE series_id = ?
        """,
        [series_id],
    ).fetchone()
    if row is None:
        return {"error": f"series not found: {series_id}"}
    keys = ["series_id", "provider", "title", "unit", "frequency", "geography", "license", "as_of", "source_url", "metadata"]
    header = dict(zip(keys, row))
    if isinstance(header.get("metadata"), str):
        try:
            header["metadata"] = json.loads(header["metadata"])
        except (ValueError, TypeError):
            header["metadata"] = {}
    summary = conn.execute(
        """
        SELECT COUNT(*), MIN(period), MAX(period)
        FROM dataset_observations
        WHERE series_id = ? AND as_of = (
            SELECT MAX(as_of) FROM dataset_observations WHERE series_id = ?
        )
        """,
        [series_id, series_id],
    ).fetchone()
    header["observation_count"] = summary[0] if summary else 0
    header["first_period"] = summary[1] if summary else None
    header["last_period"] = summary[2] if summary else None
    header["vintages"] = conn.execute(
        "SELECT COUNT(DISTINCT as_of) FROM dataset_observations WHERE series_id = ?",
        [series_id],
    ).fetchone()[0]
    return header


def get_observations(
    conn, series_id: str, as_of: Optional[int] = None, limit: int = MAX_OBS
) -> Dict[str, Any]:
    """Return observations for a series at a vintage (latest when omitted),
    capped at ``MAX_OBS`` with a truncation flag."""
    if not available(conn):
        return {"series_id": series_id, "observations": [], "truncated": False, "note": "no dataset series harvested"}
    if as_of is None:
        latest = conn.execute(
            "SELECT MAX(as_of) FROM dataset_observations WHERE series_id = ?",
            [series_id],
        ).fetchone()
        if latest is None or latest[0] is None:
            return {"series_id": series_id, "observations": [], "truncated": False}
        as_of = latest[0]
    capped = max(1, min(limit, MAX_OBS))
    rows = conn.execute(
        """
        SELECT period, value FROM dataset_observations
        WHERE series_id = ? AND as_of = ?
        ORDER BY period
        LIMIT ?
        """,
        [series_id, as_of, capped + 1],
    ).fetchall()
    truncated = len(rows) > capped
    observations = [{"period": r[0], "value": r[1]} for r in rows[:capped]]
    return {"series_id": series_id, "as_of": as_of, "observations": observations, "truncated": truncated}


def series_explorer(conn, topic: Optional[str] = None, limit: int = MAX_SERIES) -> Dict[str, Any]:
    """Panel payload: series matching an optional topic, each with a compact
    latest-vintage summary (count, range, latest value) for a spark-line."""
    listing = list_series(conn, query=topic, limit=limit)
    out: List[Dict[str, Any]] = []
    for s in listing["series"]:
        summary = conn.execute(
            """
            SELECT COUNT(*), MIN(period), MAX(period)
            FROM dataset_observations
            WHERE series_id = ? AND as_of = (
                SELECT MAX(as_of) FROM dataset_observations WHERE series_id = ?
            )
            """,
            [s["series_id"], s["series_id"]],
        ).fetchone()
        latest_val = conn.execute(
            """
            SELECT value FROM dataset_observations
            WHERE series_id = ? AND as_of = (
                SELECT MAX(as_of) FROM dataset_observations WHERE series_id = ?
            )
            ORDER BY period DESC LIMIT 1
            """,
            [s["series_id"], s["series_id"]],
        ).fetchone()
        out.append(
            {
                **s,
                "observation_count": summary[0] if summary else 0,
                "first_period": summary[1] if summary else None,
                "last_period": summary[2] if summary else None,
                "latest_value": latest_val[0] if latest_val else None,
            }
        )
    return {"series": out, "count": len(out), "truncated": listing["truncated"]}
