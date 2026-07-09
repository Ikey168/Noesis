"""
Archive-anchored citations (candidate track #790).

The evidence discipline says every line cites its source — but web sources rot.
Snapshotting cited pages at ingest makes citations durable: a citation whose URL
later 404s still resolves and renders from the snapshot, flagged as archived.
This is also the substrate the corrections track (#786) diffs against.

Snapshots are content-addressed (SHA-256 of the raw HTML) and keyed by
``(url, fetched_at)`` so revisions are retained. Stored in DuckDB; the extracted
text is kept alongside the raw HTML so a citation can render without re-parsing.

Stdlib only, connection-injected — testable offline and import-safe.

See ``docs/architecture/BEYOND_TEXT_ROADMAP.md`` §4.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS url_snapshots (
    url           TEXT NOT NULL,
    fetched_at    BIGINT NOT NULL,
    content_hash  TEXT NOT NULL,
    html          TEXT,
    text          TEXT,
    status        INTEGER,
    PRIMARY KEY (url, fetched_at)
)
"""

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_ANGLE_RE = re.compile(r"<[^>]+>")


def extract_text(html: Optional[str]) -> str:
    """Best-effort readable text from HTML (drop script/style + tags)."""
    if not html:
        return ""
    no_scripts = _TAG_RE.sub(" ", html)
    text = _ANGLE_RE.sub(" ", no_scripts)
    return re.sub(r"\s+", " ", text).strip()


def _hash(html: Optional[str]) -> str:
    return hashlib.sha256((html or "").encode("utf-8", "replace")).hexdigest()


@dataclass
class Snapshot:
    url: str
    fetched_at: int
    content_hash: str
    text: str
    status: Optional[int]
    archived: bool = True


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


class SnapshotStore:
    """DuckDB-backed archive of fetched pages."""

    def __init__(self, conn: Any):
        self._conn = conn
        self._conn.execute(_SNAPSHOTS_DDL)

    @classmethod
    def open(cls, path: str = ":memory:") -> "SnapshotStore":
        import duckdb

        return cls(duckdb.connect(path))

    def snapshot(self, url: str, html: Optional[str], fetched_at: int, status: int = 200) -> Dict[str, Any]:
        """Archive a fetched page. Idempotent per ``(url, fetched_at)``; a new
        fetch time with different content is a new snapshot (revisions kept)."""
        content_hash = _hash(html)
        text = extract_text(html)
        self._conn.execute(
            """
            INSERT INTO url_snapshots (url, fetched_at, content_hash, html, text, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (url, fetched_at) DO UPDATE SET
                content_hash = excluded.content_hash,
                html = excluded.html,
                text = excluded.text,
                status = excluded.status
            """,
            [url, fetched_at, content_hash, html, text, status],
        )
        return {"url": url, "fetched_at": fetched_at, "content_hash": content_hash, "chars": len(text)}

    def latest(self, url: str) -> Optional[Snapshot]:
        """The most recent snapshot for a URL (the archive fallback)."""
        if not _table_exists(self._conn, "url_snapshots"):
            return None
        row = self._conn.execute(
            "SELECT url, fetched_at, content_hash, text, status FROM url_snapshots WHERE url = ? ORDER BY fetched_at DESC LIMIT 1",
            [url],
        ).fetchone()
        if row is None:
            return None
        return Snapshot(url=row[0], fetched_at=row[1], content_hash=row[2], text=row[3], status=row[4])

    def snapshots(self, url: str) -> List[Snapshot]:
        rows = self._conn.execute(
            "SELECT url, fetched_at, content_hash, text, status FROM url_snapshots WHERE url = ? ORDER BY fetched_at",
            [url],
        ).fetchall()
        return [Snapshot(url=r[0], fetched_at=r[1], content_hash=r[2], text=r[3], status=r[4]) for r in rows]

    def has(self, url: str) -> bool:
        return self.latest(url) is not None


def resolve_citation(store: SnapshotStore, url: str, live_ok: bool = False) -> Dict[str, Any]:
    """Resolve a citation's URL, falling back to the archive when the live URL
    is unavailable.

    ``live_ok`` is whether the live URL currently resolves (the caller checks the
    network, kept out of this pure function). When the live link is dead, a
    snapshot keeps the citation ``cited`` rather than letting it go ``uncited``.
    """
    snap = store.latest(url)
    if live_ok:
        return {"url": url, "cited": True, "archived": snap is not None, "source": "live"}
    if snap is not None:
        return {
            "url": url,
            "cited": True,
            "archived": True,
            "source": "archive",
            "fetched_at": snap.fetched_at,
            "content_hash": snap.content_hash,
            "text": snap.text,
        }
    # Dead link and no snapshot: the flagged state the evidence discipline shows.
    return {"url": url, "cited": False, "archived": False, "source": "none"}
