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

    def snapshot_bytes(self, url, data, fetched_at, *, content_type, final_url):
        """Content-addressed binary snapshots with independent acquisition receipts."""
        if len(data)>20_000_000:
            raise ValueError('binary snapshot exceeds byte budget')
        self._conn.execute('CREATE TABLE IF NOT EXISTS source_binary_blobs(digest TEXT PRIMARY KEY, payload BLOB NOT NULL)')
        self._conn.execute('CREATE TABLE IF NOT EXISTS source_binary_observations(url TEXT, fetched_at BIGINT, digest TEXT, final_url TEXT, content_type TEXT, PRIMARY KEY(url,fetched_at,digest))')
        digest=hashlib.sha256(data).hexdigest()
        self._conn.execute('INSERT INTO source_binary_blobs VALUES(?,?) ON CONFLICT DO NOTHING',[digest,data])
        self._conn.execute('INSERT INTO source_binary_observations VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING',[url,fetched_at,digest,final_url,content_type])
        return {'url':url,'final_url':final_url,'fetched_at':fetched_at,'digest':digest,'content_type':content_type,'bytes':len(data)}

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


# --------------------------------------------------------------------------- #
# Wiring (#825): ingest-path snapshotting, liveness, retention.
#
# Posture: snapshots are an operator-side archive so *the operator's own
# citations* survive link rot — they are never republished or served to third
# parties. Snapshot only what a connector already fetched (no second fetch, so
# the robots/rate posture is whatever the fetching connector already honoured).
# --------------------------------------------------------------------------- #


def snapshot_document(store: SnapshotStore, document: Any, html: Optional[str], fetched_at: int) -> Optional[Dict[str, Any]]:
    """Archive the page a connector just fetched for ``document``.

    The ingest-path hook: connectors that pulled a URL call this with the HTML
    they already have — never a second fetch. No-op (None) for documents
    without a URL (books, uploads, notes)."""
    url = getattr(document, "url", None) or (document.get("url") if isinstance(document, dict) else None)
    if not url:
        return None
    return store.snapshot(url, html, fetched_at=fetched_at)


def check_liveness(url: str, http_head: Optional[Any] = None, timeout: float = 10.0) -> bool:
    """Whether a URL currently resolves (HEAD, 2xx/3xx). Injectable checker;
    any network failure counts as dead — the archive fallback then applies."""
    if http_head is not None:
        try:
            return bool(http_head(url))
        except Exception:  # noqa: BLE001 - a failing checker means "not live"
            return False
    try:  # pragma: no cover - trivial network shim
        import urllib.request

        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:  # noqa: BLE001
        return False


def resolve_citation_live(
    store: SnapshotStore, url: str, http_head: Optional[Any] = None
) -> Dict[str, Any]:
    """resolve_citation with the liveness check performed here: live links
    resolve live; dead links fall back to the archive; dead-and-unsnapshotted
    stays the flagged uncited state."""
    return resolve_citation(store, url, live_ok=check_liveness(url, http_head=http_head))


def prune_snapshots(
    store: SnapshotStore,
    now_ms: int,
    max_age_ms: Optional[int] = None,
    keep_latest: bool = True,
) -> int:
    """Retention: drop snapshots older than ``max_age_ms``, by default always
    keeping each URL's latest so a citation never loses its last archive copy.
    Returns rows deleted. ``max_age_ms=None`` prunes nothing."""
    if max_age_ms is None:
        return 0
    cutoff = now_ms - max_age_ms
    if keep_latest:
        result = store._conn.execute(
            """
            DELETE FROM url_snapshots
            WHERE fetched_at < ?
              AND fetched_at < (
                  SELECT MAX(s2.fetched_at) FROM url_snapshots s2
                  WHERE s2.url = url_snapshots.url
              )
            """,
            [cutoff],
        )
    else:
        result = store._conn.execute(
            "DELETE FROM url_snapshots WHERE fetched_at < ?", [cutoff]
        )
    row = result.fetchone()
    return int(row[0]) if row and row[0] is not None else 0
