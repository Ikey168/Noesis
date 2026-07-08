"""
The case-file store: durable state for the investigation engine.

Five warehouse-side tables, all outside the shared corpus (mirroring the
provisioning registry pattern):

    investigations            one row per case (question, status, verdict)
    investigation_hypotheses  the competing hypotheses a case weighs
    investigation_evidence    cited evidence rows, keyed deterministically so
                              re-pursuing a lead converges instead of duplicating
    investigation_leads       planned tool calls (open / pursued / failed),
                              keyed by a hash of (tool, params) so planning is
                              idempotent
    investigation_events      an append-only journal: every open / plan /
                              pursue / evaluate / conclude, so a case is fully
                              reconstructable

Writes run under the caller's serialising lock and the API's single warehouse
writer. Stdlib-only; the connection is injected.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

STATUS_OPEN = "open"
STATUS_ACTIVE = "active"
STATUS_CONCLUDED = "concluded"

HYPOTHESIS_ACTIVE = "active"
HYPOTHESIS_SUPPORTED = "supported"
HYPOTHESIS_UNSUPPORTED = "unsupported"

LEAD_OPEN = "open"
LEAD_PURSUED = "pursued"
LEAD_FAILED = "failed"

RELATION_SUPPORTS = "supports"
RELATION_CONTRADICTS = "contradicts"
RELATION_CONTEXT = "context"


def ensure_schema(conn) -> None:
    """Create the case-file tables if absent (idempotent)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS investigations ("
        "case_id VARCHAR PRIMARY KEY, question VARCHAR, topic VARCHAR, "
        "entities VARCHAR, status VARCHAR, verdict VARCHAR, "
        "verdict_hypothesis VARCHAR, opened_at TIMESTAMP, "
        "updated_at TIMESTAMP, concluded_at TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS investigation_hypotheses ("
        "case_id VARCHAR, hypothesis_id VARCHAR, statement VARCHAR, "
        "kind VARCHAR, status VARCHAR, created_at TIMESTAMP, "
        "PRIMARY KEY (case_id, hypothesis_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS investigation_evidence ("
        "case_id VARCHAR, evidence_id VARCHAR, hypothesis_id VARCHAR, "
        "relation VARCHAR, kind VARCHAR, ref_id VARCHAR, source VARCHAR, "
        "credibility DOUBLE, cited BOOLEAN, summary VARCHAR, lead_id VARCHAR, "
        "added_at TIMESTAMP, PRIMARY KEY (case_id, evidence_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS investigation_leads ("
        "case_id VARCHAR, lead_id VARCHAR, tool VARCHAR, params VARCHAR, "
        "rationale VARCHAR, hypothesis_id VARCHAR, status VARCHAR, "
        "evidence_found INTEGER, created_at TIMESTAMP, pursued_at TIMESTAMP, "
        "PRIMARY KEY (case_id, lead_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS investigation_events ("
        "seq BIGINT, case_id VARCHAR, event VARCHAR, detail VARCHAR, "
        "created_at TIMESTAMP)"
    )


def schema_ready(conn) -> bool:
    """True once the case tables exist (read tools may run before any case
    has been opened)."""
    try:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'investigations'"
        ).fetchall()
        return bool(rows)
    except Exception:
        return False


def slugify(text: str, max_len: int = 48) -> str:
    """A filesystem/id-safe slug of a question."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].rstrip("-") or "case"


def digest_id(prefix: str, *parts: Any) -> str:
    """A deterministic short id from its identifying parts, so re-planning a
    lead or re-harvesting the same evidence converges on one row."""
    payload = "\x1f".join(str(p) for p in parts)
    return f"{prefix}-{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12]}"


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #

def _row_to_case(row) -> Dict[str, Any]:
    entities: List[str] = []
    if row[3]:
        try:
            entities = json.loads(row[3])
        except Exception:
            entities = []
    return {
        "case_id": row[0],
        "question": row[1],
        "topic": row[2],
        "entities": entities,
        "status": row[4],
        "verdict": row[5],
        "verdict_hypothesis": row[6],
        "opened_at": str(row[7]) if row[7] is not None else None,
        "updated_at": str(row[8]) if row[8] is not None else None,
        "concluded_at": str(row[9]) if row[9] is not None else None,
    }


_CASE_COLUMNS = (
    "case_id, question, topic, entities, status, verdict, "
    "verdict_hypothesis, opened_at, updated_at, concluded_at"
)


def get_case(conn, case_id: str) -> Optional[Dict[str, Any]]:
    if not schema_ready(conn):
        return None
    row = conn.execute(
        f"SELECT {_CASE_COLUMNS} FROM investigations WHERE case_id = ?",
        [case_id],
    ).fetchone()
    return _row_to_case(row) if row else None


def list_cases(conn, include_concluded: bool = True) -> List[Dict[str, Any]]:
    if not schema_ready(conn):
        return []
    sql = f"SELECT {_CASE_COLUMNS} FROM investigations"
    params: List[Any] = []
    if not include_concluded:
        sql += " WHERE status != ?"
        params.append(STATUS_CONCLUDED)
    sql += " ORDER BY opened_at NULLS LAST, case_id"
    return [_row_to_case(r) for r in conn.execute(sql, params).fetchall()]


def insert_case(
    conn,
    case_id: str,
    question: str,
    topic: Optional[str],
    entities: List[str],
    now: Any,
) -> Dict[str, Any]:
    conn.execute(
        "INSERT INTO investigations (case_id, question, topic, entities, "
        "status, verdict, verdict_hypothesis, opened_at, updated_at, concluded_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL)",
        [case_id, question, topic, json.dumps(entities), STATUS_OPEN, now, now],
    )
    return get_case(conn, case_id)


def set_case_status(conn, case_id: str, status: str, now: Any) -> None:
    conn.execute(
        "UPDATE investigations SET status = ?, updated_at = ? WHERE case_id = ?",
        [status, now, case_id],
    )


def set_case_verdict(
    conn, case_id: str, verdict: str, hypothesis_id: str, now: Any
) -> None:
    conn.execute(
        "UPDATE investigations SET status = ?, verdict = ?, "
        "verdict_hypothesis = ?, updated_at = ?, concluded_at = ? "
        "WHERE case_id = ?",
        [STATUS_CONCLUDED, verdict, hypothesis_id, now, now, case_id],
    )


# --------------------------------------------------------------------------- #
# Hypotheses
# --------------------------------------------------------------------------- #

def add_hypothesis(
    conn,
    case_id: str,
    hypothesis_id: str,
    statement: str,
    kind: str,
    now: Any,
) -> None:
    """Add a hypothesis (idempotent by ``(case, id)``)."""
    row = conn.execute(
        "SELECT 1 FROM investigation_hypotheses "
        "WHERE case_id = ? AND hypothesis_id = ?",
        [case_id, hypothesis_id],
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO investigation_hypotheses "
            "(case_id, hypothesis_id, statement, kind, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [case_id, hypothesis_id, statement, kind, HYPOTHESIS_ACTIVE, now],
        )


def list_hypotheses(conn, case_id: str) -> List[Dict[str, Any]]:
    if not schema_ready(conn):
        return []
    rows = conn.execute(
        "SELECT hypothesis_id, statement, kind, status, created_at "
        "FROM investigation_hypotheses WHERE case_id = ? ORDER BY hypothesis_id",
        [case_id],
    ).fetchall()
    return [
        {
            "hypothesis_id": r[0],
            "statement": r[1],
            "kind": r[2],
            "status": r[3],
            "created_at": str(r[4]) if r[4] is not None else None,
        }
        for r in rows
    ]


def set_hypothesis_status(conn, case_id: str, hypothesis_id: str, status: str) -> None:
    conn.execute(
        "UPDATE investigation_hypotheses SET status = ? "
        "WHERE case_id = ? AND hypothesis_id = ?",
        [status, case_id, hypothesis_id],
    )


# --------------------------------------------------------------------------- #
# Leads
# --------------------------------------------------------------------------- #

def upsert_lead(
    conn,
    case_id: str,
    tool: str,
    params: Dict[str, Any],
    rationale: str,
    hypothesis_id: Optional[str],
    now: Any,
) -> Optional[str]:
    """Plan a lead. The lead id is a digest of ``(tool, params)`` so planning
    is idempotent: returns the new lead id, or None if the lead already
    exists (in any status - a pursued lead is never re-opened)."""
    lead_id = digest_id("lead", tool, json.dumps(params, sort_keys=True, default=str))
    row = conn.execute(
        "SELECT 1 FROM investigation_leads WHERE case_id = ? AND lead_id = ?",
        [case_id, lead_id],
    ).fetchone()
    if row is not None:
        return None
    conn.execute(
        "INSERT INTO investigation_leads "
        "(case_id, lead_id, tool, params, rationale, hypothesis_id, status, "
        "evidence_found, created_at, pursued_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL)",
        [case_id, lead_id, tool, json.dumps(params, default=str), rationale,
         hypothesis_id, LEAD_OPEN, now],
    )
    return lead_id


def list_leads(
    conn, case_id: str, status: Optional[str] = None
) -> List[Dict[str, Any]]:
    if not schema_ready(conn):
        return []
    sql = (
        "SELECT lead_id, tool, params, rationale, hypothesis_id, status, "
        "evidence_found, created_at, pursued_at "
        "FROM investigation_leads WHERE case_id = ?"
    )
    params: List[Any] = [case_id]
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at NULLS LAST, lead_id"
    out = []
    for r in conn.execute(sql, params).fetchall():
        try:
            tool_params = json.loads(r[2]) if r[2] else {}
        except Exception:
            tool_params = {}
        out.append(
            {
                "lead_id": r[0],
                "tool": r[1],
                "params": tool_params,
                "rationale": r[3],
                "hypothesis_id": r[4],
                "status": r[5],
                "evidence_found": int(r[6] or 0),
                "created_at": str(r[7]) if r[7] is not None else None,
                "pursued_at": str(r[8]) if r[8] is not None else None,
            }
        )
    return out


def get_lead(conn, case_id: str, lead_id: str) -> Optional[Dict[str, Any]]:
    for lead in list_leads(conn, case_id):
        if lead["lead_id"] == lead_id:
            return lead
    return None


def mark_lead(
    conn, case_id: str, lead_id: str, status: str, evidence_found: int, now: Any
) -> None:
    conn.execute(
        "UPDATE investigation_leads SET status = ?, evidence_found = ?, "
        "pursued_at = ? WHERE case_id = ? AND lead_id = ?",
        [status, int(evidence_found), now, case_id, lead_id],
    )


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

def upsert_evidence(
    conn,
    case_id: str,
    hypothesis_id: Optional[str],
    relation: str,
    kind: str,
    ref_id: Optional[str],
    source: str,
    credibility: Optional[float],
    cited: bool,
    summary: str,
    lead_id: Optional[str],
    now: Any,
) -> bool:
    """Attach an evidence row. Keyed by a digest of its identifying parts so
    re-pursuing the same lead converges. Returns True if newly added."""
    evidence_id = digest_id(
        "ev", hypothesis_id or "", relation, kind, ref_id or "", source
    )
    row = conn.execute(
        "SELECT 1 FROM investigation_evidence "
        "WHERE case_id = ? AND evidence_id = ?",
        [case_id, evidence_id],
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE investigation_evidence SET credibility = ?, cited = ?, "
            "summary = ? WHERE case_id = ? AND evidence_id = ?",
            [credibility, bool(cited), summary, case_id, evidence_id],
        )
        return False
    conn.execute(
        "INSERT INTO investigation_evidence "
        "(case_id, evidence_id, hypothesis_id, relation, kind, ref_id, source, "
        "credibility, cited, summary, lead_id, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [case_id, evidence_id, hypothesis_id, relation, kind, ref_id, source,
         credibility, bool(cited), summary, lead_id, now],
    )
    return True


def list_evidence(
    conn, case_id: str, hypothesis_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    if not schema_ready(conn):
        return []
    sql = (
        "SELECT evidence_id, hypothesis_id, relation, kind, ref_id, source, "
        "credibility, cited, summary, lead_id, added_at "
        "FROM investigation_evidence WHERE case_id = ?"
    )
    params: List[Any] = [case_id]
    if hypothesis_id is not None:
        sql += " AND hypothesis_id = ?"
        params.append(hypothesis_id)
    sql += " ORDER BY added_at NULLS LAST, evidence_id"
    return [
        {
            "evidence_id": r[0],
            "hypothesis_id": r[1],
            "relation": r[2],
            "kind": r[3],
            "ref_id": r[4],
            "source": r[5],
            "credibility": float(r[6]) if r[6] is not None else None,
            "cited": bool(r[7]),
            "summary": r[8],
            "lead_id": r[9],
            "added_at": str(r[10]) if r[10] is not None else None,
        }
        for r in conn.execute(sql, params).fetchall()
    ]


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #

def record_event(conn, case_id: str, event: str, detail: Dict[str, Any], now: Any) -> int:
    """Append a journal event and return its sequence number."""
    seq = int(
        conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM investigation_events"
        ).fetchone()[0]
    )
    conn.execute(
        "INSERT INTO investigation_events (seq, case_id, event, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [seq, case_id, event, json.dumps(detail, default=str), now],
    )
    return seq


def list_events(
    conn, case_id: Optional[str] = None, limit: int = 200
) -> List[Dict[str, Any]]:
    """The journal, oldest first (a case reads as a narrative), optionally
    scoped to one case."""
    if not schema_ready(conn):
        return []
    sql = "SELECT seq, case_id, event, detail, created_at FROM investigation_events"
    params: List[Any] = []
    if case_id is not None:
        sql += " WHERE case_id = ?"
        params.append(case_id)
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
                "case_id": r[1],
                "event": r[2],
                "detail": detail,
                "at": str(r[4]) if r[4] is not None else None,
            }
        )
    return list(reversed(out))
