"""
Lead-lag analysis over per-outlet coverage series (R6 / #599).

Cross-correlation of each outlet's daily coverage of a topic against every
other's finds who tends to publish *first* — distinguishing agenda-setters
from followers, a signal unique to the transparency mission. A positive lag
for the pair (A, B) means A leads B.

The fit is cheap (one topic, a handful of outlet series), so the
``lead_lag`` tool computes on-demand; a ``LeadLagJob`` is provided for the
precompute-into-a-result-table pattern when running across all topics.
Output carries the honesty envelope (n = aligned days, method,
assumptions).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.analytics.framework import AnalyticJob
from src.analytics.honesty import analytic_envelope
from src.analytics.stats import cross_correlation_lag

RESULT_TABLE = "analytics_lead_lag"
MAX_LAG = 7
MIN_OVERLAP = 4
MAX_OUTLETS = 8

METHOD = "cross-correlation lead-lag on daily coverage series"
ASSUMPTIONS = [
    "lead is correlational, not causal (no Granger causality test)",
    "needs at least %d overlapping days of coverage per outlet pair" % MIN_OVERLAP,
    "outlets with sparse coverage of the topic are dropped",
]


def _outlet_series(conn, topic: str, outlets: Optional[List[str]]):
    """Per-outlet {day: volume} for a topic, ordered outlets by total volume."""
    clauses = ["category = ?"]
    params: List[Any] = [topic]
    if outlets:
        placeholders = ", ".join("?" for _ in outlets)
        clauses.append(f"source IN ({placeholders})")
        params.extend(outlets)
    rows = conn.execute(
        f"""
        SELECT source, CAST(publish_date AS DATE) AS day, COUNT(*) AS volume
        FROM news_articles
        WHERE {' AND '.join(clauses)} AND publish_date IS NOT NULL
        GROUP BY 1, 2
        ORDER BY 1, 2
        """,
        params,
    ).fetchall()
    series: Dict[str, Dict[str, float]] = {}
    for source, day, volume in rows:
        key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        series.setdefault(str(source), {})[key] = float(volume)
    # Keep the busiest outlets so the matrix stays legible.
    ranked = sorted(series, key=lambda s: -sum(series[s].values()))
    return {s: series[s] for s in ranked[:MAX_OUTLETS]}


def _aligned(a: Dict[str, float], b: Dict[str, float]):
    days = sorted(set(a) | set(b))
    return [a.get(d, 0.0) for d in days], [b.get(d, 0.0) for d in days], len(days)


def compute_lead_lag(conn, topic: str, outlets: Optional[List[str]] = None) -> Dict[str, Any]:
    """Pairwise lead-lag matrix + a per-outlet lead score for a topic."""
    series = _outlet_series(conn, topic, outlets)
    names = list(series)
    pairs: List[Dict[str, Any]] = []
    lead_score: Dict[str, float] = {n: 0.0 for n in names}
    aligned_days = 0
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b, days = _aligned(series[names[i]], series[names[j]])
            if days < MIN_OVERLAP:
                continue
            aligned_days = max(aligned_days, days)
            res = cross_correlation_lag(a, b, MAX_LAG)
            lag, corr = res["lag"], res["correlation"]
            # Positive lag => names[i] leads names[j]; weight by correlation.
            leader, follower = names[i], names[j]
            if lag < 0:
                leader, follower = follower, leader
            pairs.append(
                {
                    "leader": leader,
                    "follower": follower,
                    "lag_days": abs(lag),
                    "correlation": round(corr, 4),
                    "overlap_days": res["overlap"],
                }
            )
            if lag != 0 and abs(corr) > 0:
                lead_score[leader] += abs(corr)
                lead_score[follower] -= abs(corr)
    ranking = sorted(
        ({"outlet": n, "lead_score": round(lead_score[n], 4)} for n in names),
        key=lambda r: -r["lead_score"],
    )
    return {"pairs": pairs, "ranking": ranking, "aligned_days": aligned_days}


def lead_lag_payload(conn, topic: str, outlets: Optional[List[str]] = None) -> Dict[str, Any]:
    """The ``lead_lag`` tool payload, honesty-wrapped."""
    if not topic:
        return {"error": "lead_lag requires a topic"}
    result = compute_lead_lag(conn, topic, outlets)
    return analytic_envelope(
        n=result["aligned_days"],
        method=METHOD,
        assumptions=ASSUMPTIONS,
        topic=topic,
        outlets=result["ranking"],
        pairs=result["pairs"],
    )


class LeadLagJob(AnalyticJob):
    """Precompute lead-lag pairs for every topic into ``analytics_lead_lag``."""

    name = "lead_lag"
    result_table = RESULT_TABLE

    def result_ddl(self) -> str:
        return f"""
            CREATE TABLE IF NOT EXISTS {RESULT_TABLE} (
                topic        VARCHAR NOT NULL,
                leader       VARCHAR NOT NULL,
                follower     VARCHAR NOT NULL,
                lag_days     INTEGER,
                correlation  DOUBLE,
                overlap_days INTEGER,
                computed_at  VARCHAR,
                PRIMARY KEY (topic, leader, follower)
            )
        """

    def compute(self, conn) -> List[Dict[str, Any]]:
        computed_at = datetime.now(timezone.utc).isoformat()
        topics = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT category FROM news_articles WHERE category IS NOT NULL"
            ).fetchall()
        ]
        out: List[Dict[str, Any]] = []
        for topic in topics:
            for pair in compute_lead_lag(conn, topic)["pairs"]:
                out.append({"topic": topic, "computed_at": computed_at, **pair})
        return out

    def store(self, conn, rows: List[Dict[str, Any]]) -> None:
        for r in rows:
            conn.execute(
                f"""INSERT OR REPLACE INTO {RESULT_TABLE}
                    (topic, leader, follower, lag_days, correlation, overlap_days, computed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [r["topic"], r["leader"], r["follower"], r["lag_days"],
                 r["correlation"], r["overlap_days"], r["computed_at"]],
            )

    def summary(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"topics": len({r["topic"] for r in rows})}
