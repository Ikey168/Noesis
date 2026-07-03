"""
Research-pack ambient telemetry (R7 / #605).

When the research pack dominates the corpus, the empty canvas shifts from
news movers to research signal: recently ingested papers (ticker), emerging
concepts by volume (movers), and headline counts. Registered on the pack via
``DomainPack.telemetry`` so it only shows while research is enabled; the
genui collector swallows any failure here.
"""

from __future__ import annotations

from typing import Any, Dict

MAX_MOVERS = 5
MAX_TICKER = 6


def research_telemetry() -> Dict[str, Any]:
    """Emerging concepts, recent papers and counts from the paper corpus."""
    from src.database.local_analytics_connector import _LOCK, get_shared_connection

    conn = get_shared_connection()
    with _LOCK:
        exists = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = 'documents'"
        ).fetchall()
        if not exists:
            return {}
        total = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE source_type = 'paper'"
        ).fetchone()[0]
        venues = conn.execute(
            "SELECT COUNT(DISTINCT venue) FROM documents "
            "WHERE source_type = 'paper' AND venue IS NOT NULL"
        ).fetchone()[0]
        concepts = conn.execute(
            "SELECT concept, COUNT(*) FROM documents "
            "WHERE source_type = 'paper' AND concept IS NOT NULL "
            "GROUP BY concept ORDER BY COUNT(*) DESC LIMIT ?",
            [MAX_MOVERS],
        ).fetchall()
        recent = conn.execute(
            "SELECT title FROM documents WHERE source_type = 'paper' "
            "ORDER BY created_at DESC NULLS LAST LIMIT ?",
            [MAX_TICKER],
        ).fetchall()

    if not total:
        return {}
    movers = [
        {
            "label": str(concept),
            "intent": f"literature on {str(concept).lower()}",
            "change": int(count),
        }
        for concept, count in concepts
        if concept
    ]
    return {
        "signals": [
            {"label": "PAPERS", "value": int(total)},
            {"label": "VENUES", "value": int(venues)},
            {"label": "EMERGING", "value": len(movers)},
        ],
        "movers": movers,
        "ticker": {
            "label": "NEW PAPERS",
            "items": [str(r[0]) for r in recent if r[0]],
        },
    }
