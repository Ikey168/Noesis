"""
News-pack ambient telemetry (R3 / Track N2).

Advertises the empty canvas's news signal — moving topics, a BREAKING
ticker, headline counts — from the warehouse. Registered on the pack via
``DomainPack.telemetry`` so the canvas only shows news telemetry while
the news pack is enabled; the genui collector swallows any failure here.
"""

from __future__ import annotations

from typing import Any, Dict

MAX_MOVERS = 5
MAX_TICKER = 6


def news_telemetry() -> Dict[str, Any]:
    """Movers, ticker and KPI signals from the news_articles table."""
    from src.database.local_analytics_connector import _LOCK, get_shared_connection

    conn = get_shared_connection()
    with _LOCK:
        total = conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
        sources = conn.execute(
            "SELECT COUNT(DISTINCT source) FROM news_articles"
        ).fetchone()[0]
        movers_rows = conn.execute(
            """
            SELECT category, COUNT(*) AS n,
                   COUNT(*) FILTER (
                       WHERE publish_date >= CURRENT_TIMESTAMP - INTERVAL 7 DAY
                   ) AS recent
            FROM news_articles
            GROUP BY category ORDER BY n DESC LIMIT ?
            """,
            [MAX_MOVERS],
        ).fetchall()
        ticker_rows = conn.execute(
            "SELECT title FROM news_articles "
            "ORDER BY publish_date DESC NULLS LAST LIMIT ?",
            [MAX_TICKER],
        ).fetchall()

    movers = []
    for category, count, recent in movers_rows:
        if not category:
            continue
        share = round(100 * (recent or 0) / count) if count else 0
        movers.append(
            {
                "label": str(category),
                "intent": f"coverage of {str(category).lower()}",
                "change": share,
            }
        )
    return {
        "signals": [
            {"label": "ARTICLES", "value": int(total)},
            {"label": "SOURCES", "value": int(sources)},
            {"label": "TOPICS MOVING", "value": len(movers)},
        ],
        "movers": movers,
        "ticker": {
            "label": "BREAKING",
            "items": [str(r[0]) for r in ticker_rows if r[0]],
        },
    }
