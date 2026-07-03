"""
Shared read helpers for the OSINT composition tools.

All three tools need the same two joins: from a claim to the *source* that
carried it (``argument_claims.document_id`` to ``news_articles.source``), and
from a source to its *credibility* (the latest ``outlet_scores.composite_score``).
Kept in one place so corroboration, reliability and contradiction-scan agree on
what "source" and "credibility" mean.

Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# Credibility assumed for a source with no transparency score yet. Neutral, so
# an unscored source neither helps nor hurts a corroboration tally.
DEFAULT_CREDIBILITY = 0.5


def table_exists(conn, table: str) -> bool:
    try:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall()
        return bool(rows)
    except Exception:
        return False


def claim_sources(conn, claim_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Map each claim_id to its carrying source: ``{source, source_type, url,
    document_id}``. Resolves the outlet name via ``news_articles`` when the
    claim's document is a news article, else falls back to the claim's
    ``source_type`` as the source label."""
    if not claim_ids or not table_exists(conn, "argument_claims"):
        return {}
    ph = ", ".join("?" for _ in claim_ids)
    has_articles = table_exists(conn, "news_articles")
    if has_articles:
        rows = conn.execute(
            f"""
            SELECT c.claim_id, c.source_type, c.document_id,
                   a.source, a.url
            FROM argument_claims c
            LEFT JOIN news_articles a ON c.document_id = a.id
            WHERE c.claim_id IN ({ph})
            """,
            list(claim_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT claim_id, source_type, document_id, NULL, NULL "
            f"FROM argument_claims WHERE claim_id IN ({ph})",
            list(claim_ids),
        ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        claim_id, source_type, document_id, source, url = r
        out[claim_id] = {
            "source": source or source_type or "unknown",
            "source_type": source_type,
            "document_id": document_id,
            "url": url,
            # True only when the claim's document resolved to a real corpus
            # article; a dangling document_id does not count as a citation.
            "resolved": source is not None,
        }
    return out


def source_credibility(conn, sources: Sequence[str]) -> Dict[str, Optional[float]]:
    """Latest ``composite_score`` per source from ``outlet_scores`` (None when
    the source has never been scored)."""
    uniq = [s for s in dict.fromkeys(sources) if s]
    if not uniq or not table_exists(conn, "outlet_scores"):
        return {s: None for s in uniq}
    ph = ", ".join("?" for _ in uniq)
    rows = conn.execute(
        f"""
        SELECT source, composite_score
        FROM outlet_scores o
        WHERE source IN ({ph})
          AND score_date = (
            SELECT MAX(score_date) FROM outlet_scores i WHERE i.source = o.source
          )
        """,
        uniq,
    ).fetchall()
    scored = {r[0]: (float(r[1]) if r[1] is not None else None) for r in rows}
    return {s: scored.get(s) for s in uniq}


def credibility_or_default(value: Optional[float]) -> float:
    """A usable credibility weight for tallies (neutral default when unscored)."""
    return DEFAULT_CREDIBILITY if value is None else value


def dedupe_sources(entries: List[Dict[str, Any]]) -> List[str]:
    """Distinct source names in a list of ``{source: ...}`` rows."""
    return list(dict.fromkeys(e["source"] for e in entries if e.get("source")))
