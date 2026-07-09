"""
Staged re-fetch scheduler for corrections tracking (#824).

The corrections core (``corrections.py``) classifies how a document changed —
but nothing *feeds* it. This module selects already-ingested documents whose
age has crossed a re-fetch stage (default 1d / 7d / 30d after ingest), pulls
their current content, and records revisions; when a snapshot store is
supplied, each re-fetch also archives the page (#825 — one fetch serves both).

Politeness: per-run and per-domain caps bound each run; the fetcher is
injectable so scheduling logic is offline-testable and the runner never
hard-codes a network client. A fetch failure skips the document (logged),
never aborts the run.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from src.ingestion.corrections import ensure_schema, record_revision
from src.ingestion.snapshots import SnapshotStore, snapshot_document

logger = logging.getLogger(__name__)

_DAY_MS = 24 * 60 * 60 * 1000
# Staged re-fetch offsets after ingest (issue #824): 1d, 7d, 30d.
DEFAULT_STAGES_MS = (1 * _DAY_MS, 7 * _DAY_MS, 30 * _DAY_MS)

DEFAULT_LIMIT = 50
DEFAULT_PER_DOMAIN = 5

# Fetcher signature: url -> page content (str) or None on failure.
Fetcher = Callable[[str], Optional[str]]


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


def due_documents(
    conn,
    now_ms: int,
    stages_ms: tuple = DEFAULT_STAGES_MS,
    limit: int = DEFAULT_LIMIT,
) -> List[Dict[str, Any]]:
    """Documents whose age crossed a re-fetch stage not yet performed.

    A document is due for stage ``k`` when ``now - ingested_at >= stages[k]``
    and no revision has been recorded at-or-after ``ingested_at + stages[k]``
    (revision 0, the ingest baseline, never satisfies a stage). Returns the
    earliest-ingested first, capped at ``limit``.
    """
    if not _table_exists(conn, "documents"):
        return []
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT d.document_id, d.url, d.ingested_at
        FROM documents d
        WHERE d.url IS NOT NULL AND d.ingested_at IS NOT NULL
        ORDER BY d.ingested_at
        """
    ).fetchall()
    due: List[Dict[str, Any]] = []
    for document_id, url, ingested_at in rows:
        if len(due) >= limit:
            break
        for stage_index, offset in enumerate(stages_ms):
            stage_at = int(ingested_at) + int(offset)
            if now_ms < stage_at:
                break  # later stages are further out
            done = conn.execute(
                """
                SELECT COUNT(*) FROM document_revisions
                WHERE document_id = ? AND revision > 0 AND fetched_at >= ?
                """,
                [document_id, stage_at],
            ).fetchone()[0]
            if done == 0:
                due.append({
                    "document_id": document_id,
                    "url": url,
                    "ingested_at": int(ingested_at),
                    "stage": stage_index,
                    "stage_at": stage_at,
                })
                break  # one stage per run per document
    return due


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def run_refetch(
    conn,
    fetcher: Fetcher,
    now_ms: int,
    stages_ms: tuple = DEFAULT_STAGES_MS,
    limit: int = DEFAULT_LIMIT,
    per_domain: int = DEFAULT_PER_DOMAIN,
    snapshot_store: Optional[SnapshotStore] = None,
) -> Dict[str, Any]:
    """One scheduled re-fetch pass.

    Fetches each due document's URL (respecting the per-domain cap), records a
    revision when the content changed, and — when a snapshot store is supplied
    — archives the fetched page in the same pass. A fetch failure or a fetcher
    returning None skips that document (logged), never aborts the run.
    """
    due = due_documents(conn, now_ms, stages_ms=stages_ms, limit=limit)
    domain_counts: Dict[str, int] = {}
    checked = 0
    skipped = 0
    by_class: Dict[str, int] = {}
    for item in due:
        domain = _domain(item["url"])
        if domain_counts.get(domain, 0) >= per_domain:
            skipped += 1
            continue
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        try:
            content = fetcher(item["url"])
        except Exception:  # noqa: BLE001 - one bad URL never aborts the run
            logger.warning("refetch: fetch failed for %s", item["url"], exc_info=True)
            content = None
        if content is None:
            skipped += 1
            continue
        checked += 1
        result = record_revision(conn, item["document_id"], content, fetched_at=now_ms)
        change = result["change_class"]
        by_class[change] = by_class.get(change, 0) + 1
        if snapshot_store is not None:
            snapshot_document(snapshot_store, {"url": item["url"]}, content, fetched_at=now_ms)
    return {
        "due": len(due),
        "checked": checked,
        "skipped": skipped,
        "changed": sum(v for k, v in by_class.items() if k != "unchanged"),
        "by_class": by_class,
    }


def main() -> None:  # pragma: no cover - thin CLI wrapper
    """Run one re-fetch pass against the warehouse: python -m src.ingestion.refetch"""
    import os
    import time
    import urllib.request

    import duckdb

    from src.config.env import warehouse_path

    path = warehouse_path(os.path.join("data", "neuronews.duckdb"))
    conn = duckdb.connect(path)

    def fetcher(url: str) -> Optional[str]:
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
                return resp.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return None

    store = SnapshotStore(conn)
    summary = run_refetch(conn, fetcher, now_ms=int(time.time() * 1000), snapshot_store=store)
    print(summary)


if __name__ == "__main__":  # pragma: no cover
    main()
