"""
Corrections and retractions tracking (candidate track #786).

Outlets silently edit articles; journals retract papers. Detecting that the
record changed under you is temporal evidence about source reliability that
nothing else in the pipeline captures. This module re-fetches already-ingested
URLs, diffs the new content against the stored version, classifies the change,
and records a revision history — silent substantive edits become a
ledger-style finding citing both versions.

Pure/stdlib and connection-injected, so it is testable offline and import-safe.

See ``docs/architecture/BEYOND_TEXT_ROADMAP.md`` §4.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Change classes, most-to-least severe for a reliability signal.
CORRECTION_NOTICE = "correction_notice"  # the outlet flagged the change
RETRACTION = "retraction"                # a paper/article was retracted
TAKEDOWN = "takedown"                    # content removed/blanked
SILENT_SUBSTANTIVE = "silent_substantive"  # meaning changed, no notice
COSMETIC = "cosmetic"                    # whitespace/markup only
UNCHANGED = "unchanged"

_NOTICE_RE = re.compile(
    r"\b(correction|corrected|clarification|clarif(y|ied)|editor'?s note|"
    r"updated to reflect|an earlier version)\b",
    re.IGNORECASE,
)
_RETRACTION_RE = re.compile(r"\b(retract(ed|ion)|withdrawn|this (article|paper) has been removed)\b", re.IGNORECASE)

_REVISIONS_DDL = """
CREATE TABLE IF NOT EXISTS document_revisions (
    document_id   TEXT NOT NULL,
    revision      INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    content       TEXT,
    change_class  TEXT,
    fetched_at    BIGINT,
    PRIMARY KEY (document_id, revision)
)
"""


@dataclass
class DiffResult:
    change_class: str
    similarity: float  # 0..1 over normalized text
    added_chars: int
    removed_chars: int
    notice: bool
    retraction: bool


def _normalize(text: Optional[str]) -> str:
    """Collapse whitespace and drop markup so cosmetic edits compare equal."""
    if not text:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", no_tags).strip().lower()


def _hash(text: Optional[str]) -> str:
    return hashlib.sha256((_normalize(text)).encode()).hexdigest()[:16]


def classify_change(old: Optional[str], new: Optional[str]) -> DiffResult:
    """Classify how ``new`` differs from ``old``."""
    n_old, n_new = _normalize(old), _normalize(new)
    notice = bool(_NOTICE_RE.search(new or ""))
    retraction = bool(_RETRACTION_RE.search(new or ""))
    if n_old == n_new:
        cls = UNCHANGED
    elif len(n_new) < 15 or (len(n_old) > 200 and len(n_new) < 0.25 * len(n_old)):
        cls = TAKEDOWN  # content blanked, or a substantial article gutted
    elif retraction:
        cls = RETRACTION
    elif notice:
        cls = CORRECTION_NOTICE
    else:
        # Cosmetic if raw differs but normalized text is identical (handled
        # above); here normalized text changed -> substantive.
        cls = SILENT_SUBSTANTIVE

    similarity = difflib.SequenceMatcher(None, n_old, n_new).ratio() if (n_old or n_new) else 1.0
    added = max(0, len(n_new) - len(n_old))
    removed = max(0, len(n_old) - len(n_new))
    # A tiny normalized change with no notice below a threshold is cosmetic.
    if cls == SILENT_SUBSTANTIVE and similarity > 0.995:
        cls = COSMETIC
    return DiffResult(cls, round(similarity, 4), added, removed, notice, retraction)


def ensure_schema(conn) -> None:
    conn.execute(_REVISIONS_DDL)


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


def record_revision(conn, document_id: str, content: Optional[str], fetched_at: Optional[int] = None) -> Dict[str, Any]:
    """Append a revision if the content changed from the latest stored one.

    Returns the change classification. The first observation of a document is
    revision 0 (``unchanged`` baseline); subsequent differing fetches append a
    new revision carrying the classification vs. the prior content.
    """
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT revision, content FROM document_revisions
        WHERE document_id = ? ORDER BY revision DESC LIMIT 1
        """,
        [document_id],
    ).fetchone()
    new_hash = _hash(content)
    if row is None:
        conn.execute(
            "INSERT INTO document_revisions VALUES (?, 0, ?, ?, ?, ?)",
            [document_id, new_hash, content, UNCHANGED, fetched_at],
        )
        from src.kb.temporal import record_revision_time

        record_revision_time(conn, document_id, fetched_at, UNCHANGED)
        return {"document_id": document_id, "revision": 0, "change_class": UNCHANGED}
    prev_rev, prev_content = row
    diff = classify_change(prev_content, content)
    if diff.change_class == UNCHANGED:
        return {"document_id": document_id, "revision": prev_rev, "change_class": UNCHANGED}
    revision = prev_rev + 1
    conn.execute(
        "INSERT INTO document_revisions VALUES (?, ?, ?, ?, ?, ?)",
        [document_id, revision, new_hash, content, diff.change_class, fetched_at],
    )
    from src.kb.temporal import record_revision_time

    record_revision_time(conn, document_id, fetched_at, diff.change_class)
    return {
        "document_id": document_id,
        "revision": revision,
        "change_class": diff.change_class,
        "similarity": diff.similarity,
    }


def revision_history(conn, document_id: str) -> List[Dict[str, Any]]:
    if not _table_exists(conn, "document_revisions"):
        return []
    rows = conn.execute(
        "SELECT revision, content_hash, change_class, fetched_at FROM document_revisions WHERE document_id = ? ORDER BY revision",
        [document_id],
    ).fetchall()
    keys = ["revision", "content_hash", "change_class", "fetched_at"]
    return [dict(zip(keys, r)) for r in rows]


# Substantive change classes that belong on the corrections ledger.
_LEDGER_CLASSES = (CORRECTION_NOTICE, RETRACTION, TAKEDOWN, SILENT_SUBSTANTIVE)


def corrections_ledger(conn, change_class: Optional[str] = None, limit: int = 40) -> Dict[str, Any]:
    """Documents whose record changed after ingest, each citing the revision
    pair. Defaults to all substantive change classes; the silent ones are the
    signal outlet scoring cares about most."""
    if not _table_exists(conn, "document_revisions"):
        return {"entries": [], "count": 0, "note": "no revisions recorded"}
    clauses = ["change_class != 'unchanged'"]
    params: List[Any] = []
    if change_class:
        clauses = ["change_class = ?"]
        params = [change_class]
    else:
        clauses = [f"change_class IN ({','.join(['?'] * len(_LEDGER_CLASSES))})"]
        params = list(_LEDGER_CLASSES)
    capped = max(1, min(limit, 200))
    rows = conn.execute(
        f"""
        SELECT document_id, revision, change_class, fetched_at
        FROM document_revisions
        WHERE {' AND '.join(clauses)}
        ORDER BY fetched_at DESC NULLS LAST, document_id, revision
        LIMIT {capped}
        """,
        params,
    ).fetchall()
    keys = ["document_id", "revision", "change_class", "fetched_at"]
    entries = [dict(zip(keys, r)) for r in rows]
    return {"entries": entries, "count": len(entries)}


def reliability_signal(conn, document_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Per-document correction behaviour as an input to outlet/venue scoring:
    counts of each change class. Silent substantive edits and takedowns lower
    trust; flagged corrections are neutral-to-positive (the outlet disclosed)."""
    if not _table_exists(conn, "document_revisions"):
        return {"by_class": {}, "documents_with_changes": 0}
    rows = conn.execute(
        "SELECT change_class, COUNT(*) FROM document_revisions WHERE change_class != 'unchanged' GROUP BY change_class"
    ).fetchall()
    by_class = {r[0]: r[1] for r in rows}
    changed = conn.execute(
        "SELECT COUNT(DISTINCT document_id) FROM document_revisions WHERE change_class != 'unchanged'"
    ).fetchone()[0]
    return {"by_class": by_class, "documents_with_changes": changed}
