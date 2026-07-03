"""
The provisioning registry and lineage event log (R8 #606 / #608).

Three warehouse-side tables, all outside the shared corpus:

    provisioned_kgs         one row per deployed KG (name, description, status)
    provisioned_kg_sources  the sources bound to each KG, with why they were
                            selected
    provisioning_events     an append-only lineage log: every deploy / attach /
                            ingest / teardown, so each step is visible in
                            lineage (the R8 provenance guardrail)

All writes are idempotent upserts keyed by name (or by ``(kg, source)``), so
re-running a failed provision converges instead of duplicating. Writes run
under the caller's serialising lock and the API's single warehouse writer.

Stdlib-only; the connection is injected.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

STATUS_DEPLOYED = "deployed"
STATUS_ARCHIVED = "archived"


def ensure_schema(conn) -> None:
    """Create the registry and lineage tables if absent (idempotent)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS provisioned_kgs ("
        "name VARCHAR PRIMARY KEY, description VARCHAR, ontology VARCHAR, "
        "status VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP, "
        "last_ingest_at TIMESTAMP, backend VARCHAR, db_path VARCHAR)"
    )
    # Migrate an R8/R9 registry that predates the backend columns (P2 / #640).
    for col, decl in (("backend", "VARCHAR"), ("db_path", "VARCHAR")):
        try:
            conn.execute(f"ALTER TABLE provisioned_kgs ADD COLUMN IF NOT EXISTS {col} {decl}")
        except Exception:
            pass
    conn.execute(
        "CREATE TABLE IF NOT EXISTS provisioned_kg_sources ("
        "kg_name VARCHAR, source VARCHAR, source_type VARCHAR, reason VARCHAR, "
        "attached_at TIMESTAMP, PRIMARY KEY (kg_name, source))"
    )
    # Bound pipelines (connectors/feeds) per KG (P2 / #641).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS provisioned_kg_pipelines ("
        "kg_name VARCHAR, connector VARCHAR, connector_type VARCHAR, "
        "config VARCHAR, contract VARCHAR, attached_at TIMESTAMP, "
        "PRIMARY KEY (kg_name, connector))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS provisioning_events ("
        "seq BIGINT, kg_name VARCHAR, event VARCHAR, detail VARCHAR, "
        "created_at TIMESTAMP)"
    )


def schema_ready(conn) -> bool:
    """True once the registry tables exist (read tools may run before any
    deploy has created them)."""
    try:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'provisioned_kgs'"
        ).fetchall()
        return bool(rows)
    except Exception:
        return False


def _row_to_kg(row) -> Dict[str, Any]:
    ontology: Any = None
    if row[2]:
        try:
            ontology = json.loads(row[2])
        except Exception:
            ontology = None
    return {
        "name": row[0],
        "description": row[1],
        "ontology": ontology,
        "status": row[3],
        "created_at": str(row[4]) if row[4] is not None else None,
        "updated_at": str(row[5]) if row[5] is not None else None,
        "last_ingest_at": str(row[6]) if row[6] is not None else None,
        "backend": (row[7] if len(row) > 7 and row[7] else "table-prefix"),
        "db_path": (row[8] if len(row) > 8 else None),
    }


_KG_COLUMNS = (
    "name, description, ontology, status, created_at, updated_at, "
    "last_ingest_at, backend, db_path"
)


def get_kg(conn, name: str) -> Optional[Dict[str, Any]]:
    """Return the KG record for ``name``, or None."""
    if not schema_ready(conn):
        return None
    row = conn.execute(
        f"SELECT {_KG_COLUMNS} FROM provisioned_kgs WHERE name = ?",
        [name],
    ).fetchone()
    return _row_to_kg(row) if row else None


def list_kgs(conn, include_archived: bool = False) -> List[Dict[str, Any]]:
    """List KGs, deployed-only by default."""
    if not schema_ready(conn):
        return []
    sql = f"SELECT {_KG_COLUMNS} FROM provisioned_kgs"
    params: List[Any] = []
    if not include_archived:
        sql += " WHERE status = ?"
        params.append(STATUS_DEPLOYED)
    sql += " ORDER BY created_at NULLS LAST, name"
    return [_row_to_kg(r) for r in conn.execute(sql, params).fetchall()]


def count_deployed(conn) -> int:
    """How many KGs are currently deployed (for the max-KGs quota)."""
    if not schema_ready(conn):
        return 0
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM provisioned_kgs WHERE status = ?",
            [STATUS_DEPLOYED],
        ).fetchone()[0]
    )


def upsert_kg(
    conn,
    name: str,
    description: str,
    ontology: Any,
    now: Any,
    status: str = STATUS_DEPLOYED,
    backend: str = "table-prefix",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert or update a KG keyed by name (idempotent). Preserves the original
    ``created_at`` and the ``backend``/``db_path`` on re-deploy so a converging
    re-run does not reset them."""
    ontology_json = json.dumps(ontology) if ontology is not None else None
    existing = get_kg(conn, name)
    if existing is None:
        conn.execute(
            "INSERT INTO provisioned_kgs "
            "(name, description, ontology, status, created_at, updated_at, "
            "last_ingest_at, backend, db_path) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            [name, description, ontology_json, status, now, now, backend, db_path],
        )
    else:
        conn.execute(
            "UPDATE provisioned_kgs SET description = ?, ontology = ?, "
            "status = ?, updated_at = ? WHERE name = ?",
            [description, ontology_json, status, now, name],
        )
    return get_kg(conn, name)


def set_status(conn, name: str, status: str, now: Any) -> None:
    """Set a KG's lifecycle status."""
    conn.execute(
        "UPDATE provisioned_kgs SET status = ?, updated_at = ? WHERE name = ?",
        [status, now, name],
    )


def mark_ingested(conn, name: str, now: Any) -> None:
    """Stamp the last-ingest time (feeds the ingest rate-cap guardrail)."""
    conn.execute(
        "UPDATE provisioned_kgs SET last_ingest_at = ?, updated_at = ? "
        "WHERE name = ?",
        [now, now, name],
    )


def count_sources(conn, name: str) -> int:
    """How many sources are bound to a KG (for the max-sources quota)."""
    if not schema_ready(conn):
        return 0
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM provisioned_kg_sources WHERE kg_name = ?",
            [name],
        ).fetchone()[0]
    )


def upsert_source(
    conn,
    name: str,
    source: str,
    source_type: Optional[str],
    reason: str,
    now: Any,
) -> bool:
    """Bind a source to a KG (idempotent by ``(kg, source)``). Returns True if
    the source was newly added, False if it was already bound (updated)."""
    row = conn.execute(
        "SELECT 1 FROM provisioned_kg_sources WHERE kg_name = ? AND source = ?",
        [name, source],
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO provisioned_kg_sources "
            "(kg_name, source, source_type, reason, attached_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [name, source, source_type, reason, now],
        )
        return True
    conn.execute(
        "UPDATE provisioned_kg_sources SET source_type = ?, reason = ? "
        "WHERE kg_name = ? AND source = ?",
        [source_type, reason, name, source],
    )
    return False


def list_sources(conn, name: str) -> List[Dict[str, Any]]:
    """The sources bound to a KG, with why each was selected."""
    if not schema_ready(conn):
        return []
    rows = conn.execute(
        "SELECT source, source_type, reason, attached_at "
        "FROM provisioned_kg_sources WHERE kg_name = ? ORDER BY source",
        [name],
    ).fetchall()
    return [
        {
            "source": r[0],
            "source_type": r[1],
            "reason": r[2],
            "attached_at": str(r[3]) if r[3] is not None else None,
        }
        for r in rows
    ]


def detach_all_sources(conn, name: str) -> int:
    """Unbind every source from a KG (teardown). Returns the count removed."""
    n = count_sources(conn, name)
    conn.execute("DELETE FROM provisioned_kg_sources WHERE kg_name = ?", [name])
    return n


# --------------------------------------------------------------------------- #
# Bound pipelines (P2 / #641)
# --------------------------------------------------------------------------- #

def count_pipelines(conn, name: str) -> int:
    """How many pipelines are bound to a KG (for the max-pipelines quota)."""
    if not schema_ready(conn):
        return 0
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM provisioned_kg_pipelines WHERE kg_name = ?",
            [name],
        ).fetchone()[0]
    )


def upsert_pipeline(
    conn,
    name: str,
    connector: str,
    connector_type: str,
    config: Any,
    contract: Optional[str],
    now: Any,
) -> bool:
    """Bind a pipeline (connector/feed) to a KG (idempotent by ``(kg,
    connector)``). Returns True if newly bound, False if it was already bound
    (updated)."""
    config_json = json.dumps(config, default=str) if config is not None else None
    row = conn.execute(
        "SELECT 1 FROM provisioned_kg_pipelines WHERE kg_name = ? AND connector = ?",
        [name, connector],
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO provisioned_kg_pipelines "
            "(kg_name, connector, connector_type, config, contract, attached_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [name, connector, connector_type, config_json, contract, now],
        )
        return True
    conn.execute(
        "UPDATE provisioned_kg_pipelines SET connector_type = ?, config = ?, "
        "contract = ? WHERE kg_name = ? AND connector = ?",
        [connector_type, config_json, contract, name, connector],
    )
    return False


def list_pipelines(conn, name: str) -> List[Dict[str, Any]]:
    """The pipelines bound to a KG."""
    if not schema_ready(conn):
        return []
    rows = conn.execute(
        "SELECT connector, connector_type, config, contract, attached_at "
        "FROM provisioned_kg_pipelines WHERE kg_name = ? ORDER BY connector",
        [name],
    ).fetchall()
    out = []
    for r in rows:
        try:
            config = json.loads(r[2]) if r[2] else {}
        except Exception:
            config = {}
        out.append(
            {
                "connector": r[0],
                "connector_type": r[1],
                "config": config,
                "contract": r[3],
                "attached_at": str(r[4]) if r[4] is not None else None,
            }
        )
    return out


def detach_all_pipelines(conn, name: str) -> int:
    """Unbind every pipeline from a KG (teardown). Returns the count removed."""
    n = count_pipelines(conn, name)
    conn.execute("DELETE FROM provisioned_kg_pipelines WHERE kg_name = ?", [name])
    return n


def record_event(conn, name: str, event: str, detail: Dict[str, Any], now: Any) -> int:
    """Append a lineage event and return its sequence number."""
    seq = int(
        conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM provisioning_events"
        ).fetchone()[0]
    )
    conn.execute(
        "INSERT INTO provisioning_events (seq, kg_name, event, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [seq, name, event, json.dumps(detail, default=str), now],
    )
    return seq


def list_events(
    conn, name: Optional[str] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    """The lineage event log, newest first, optionally scoped to one KG."""
    if not schema_ready(conn):
        return []
    sql = "SELECT seq, kg_name, event, detail, created_at FROM provisioning_events"
    params: List[Any] = []
    if name is not None:
        sql += " WHERE kg_name = ?"
        params.append(name)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(int(limit))
    out = []
    for r in conn.execute(sql, params).fetchall():
        try:
            detail = json.loads(r[3]) if r[3] else {}
        except Exception:
            detail = {}
        out.append(
            {
                "seq": int(r[0]),
                "kg": r[1],
                "event": r[2],
                "detail": detail,
                "at": str(r[4]) if r[4] is not None else None,
            }
        )
    return out
