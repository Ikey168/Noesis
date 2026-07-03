"""
The evidence discipline, shared by every OSINT investigation tool (R10 #614,
enforced across R11 #615-#617).

The non-negotiable rule: no fact renders without a citation to its source
document, "uncited" is a visible state (flagged, never hidden), and
corroboration is an explicit independent-source count rather than a single
asserted confidence number. This module is the one place those rules live, so
the dossier, path and timeline tools cite identically.

* :func:`citation` builds the ``{document_id, source, url, path, cited}``
  locator every rendered line carries; ``cited`` is False when the document
  does not resolve to the corpus (the flagged state).
* :func:`render_state` maps an independent-source count to ``cited`` /
  ``single_sourced`` / ``uncited`` for a panel to badge.

Stdlib-only; a read-only connection is injected by callers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from src.osint import common


def citation(
    document_id: Optional[str],
    source: Optional[str],
    url: Optional[str],
    chunk: Optional[str] = None,
    resolved: Optional[bool] = None,
) -> Dict[str, Any]:
    """A source locator for one rendered line. ``path`` is the citation path
    (document id plus optional chunk); ``cited`` is True only when the line
    resolves to a real corpus document."""
    is_cited = bool(document_id) if resolved is None else bool(resolved)
    path = None
    if document_id:
        path = f"{document_id}#{chunk}" if chunk else str(document_id)
    return {
        "document_id": document_id,
        "source": source or "unknown",
        "url": url,
        "path": path,
        "cited": is_cited,
    }


def render_state(independent_sources: int) -> str:
    """The evidence render state for a fact backed by ``independent_sources``
    distinct sources: none is ``uncited``, one is ``single_sourced``, more is
    ``cited`` (corroborated)."""
    if independent_sources <= 0:
        return "uncited"
    if independent_sources == 1:
        return "single_sourced"
    return "cited"


def document_citations(conn, document_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Resolve document ids to citations via ``news_articles``. A document id
    with no matching article yields a citation with ``cited=False`` so the gap
    is visible."""
    uniq = [d for d in dict.fromkeys(document_ids) if d]
    out: Dict[str, Dict[str, Any]] = {}
    resolved: Dict[str, Any] = {}
    if uniq and common.table_exists(conn, "news_articles"):
        ph = ", ".join("?" for _ in uniq)
        for r in conn.execute(
            f"SELECT id, source, url, title FROM news_articles WHERE id IN ({ph})",
            uniq,
        ).fetchall():
            resolved[r[0]] = {"source": r[1], "url": r[2], "title": r[3]}
    for d in uniq:
        info = resolved.get(d)
        out[d] = citation(
            d,
            (info or {}).get("source"),
            (info or {}).get("url"),
            resolved=info is not None,
        )
        if info is not None:
            out[d]["title"] = info.get("title")
    return out


def uncited_count(rows: List[Dict[str, Any]], key: str = "cited") -> int:
    """How many rows are in the uncited (flagged) state."""
    return sum(1 for r in rows if not r.get(key))
