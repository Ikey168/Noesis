"""
Anomaly detection over coverage series (R5 / Track DS Wave 1a, #597).

Robust z-scores (median / MAD) over per-topic daily coverage **volume** and
mean **sentiment** series flag windows that deviate from the topic's own
recent behaviour — the single most useful "what should I look at?" signal
for a news terminal, and cheap and statistically honest.

The fit runs through the batch framework (:class:`AnomalyJob` writes the
``analytics_anomalies`` result table, logged to MLflow); the
``detect_anomalies`` MCP tool reads that table, computing on-demand only
for a single requested topic when nothing is precomputed. Output always
carries the honesty envelope (``n`` / ``method`` / ``assumptions``).

Pure-stdlib maths (``src.analytics.stats``): no numpy/scipy needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.analytics.framework import AnalyticJob, read_results
from src.analytics.honesty import analytic_envelope, interval
from src.analytics.stats import mad, median, robust_z_scores
from src.database.news_articles_compat import corpus_table

RESULT_TABLE = "analytics_anomalies"
DEFAULT_THRESHOLD = 3.5
MIN_POINTS = 5
METRICS = ("volume", "sentiment")
_MAD_SCALE = 1.4826

METHOD = "robust z-score (median/MAD) over per-topic daily series"
ASSUMPTIONS = [
    "each topic's series is judged against its own recent history",
    "windows need at least %d days of data; sparser topics are skipped" % MIN_POINTS,
    "robust to outliers but assumes a roughly stable baseline (no strong trend)",
]


def _series_by_topic(conn) -> Dict[str, List[Dict[str, Any]]]:
    """Per-topic daily (date, volume, sentiment) points, ordered by date."""
    rows = conn.execute(
        f"""
        SELECT category AS topic,
               CAST(publish_date AS DATE) AS day,
               COUNT(*) AS volume,
               AVG(sentiment_score) AS sentiment
        FROM {corpus_table(conn)}
        WHERE category IS NOT NULL AND publish_date IS NOT NULL
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    ).fetchall()
    series: Dict[str, List[Dict[str, Any]]] = {}
    for topic, day, volume, sentiment in rows:
        series.setdefault(str(topic), []).append(
            {
                "day": day.isoformat() if hasattr(day, "isoformat") else str(day),
                "volume": float(volume),
                "sentiment": float(sentiment) if sentiment is not None else 0.0,
            }
        )
    return series


def compute_anomalies(
    conn,
    threshold: float = DEFAULT_THRESHOLD,
    min_points: int = MIN_POINTS,
    topic: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Compute anomaly rows for every topic (or one ``topic``) and metric.

    Returns one row per (topic, metric, day) carrying the observed value, its
    robust z-score and an anomaly flag, plus the series' median/MAD so the
    panel can draw the expected band. Read-only against the corpus
    (``corpus_documents`` when present, else ``news_articles``).
    """
    computed_at = datetime.now(timezone.utc).isoformat()
    out: List[Dict[str, Any]] = []
    for series_topic, points in _series_by_topic(conn).items():
        if topic is not None and series_topic != topic:
            continue
        if len(points) < min_points:
            continue
        for metric in METRICS:
            values = [p[metric] for p in points]
            med = median(values)
            spread = mad(values)
            zs = robust_z_scores(values)
            for point, value, z in zip(points, values, zs):
                out.append(
                    {
                        "topic": series_topic,
                        "metric": metric,
                        "window_date": point["day"],
                        "value": value,
                        "robust_z": z,
                        "is_anomaly": abs(z) > threshold,
                        "n_points": len(values),
                        "series_median": med,
                        "series_mad": spread,
                        "threshold": threshold,
                        "computed_at": computed_at,
                    }
                )
    return out


class AnomalyJob(AnalyticJob):
    """Batch job: fit anomaly scores for every topic into
    ``analytics_anomalies`` (the reference analytic for R5 #595)."""

    name = "detect_anomalies"
    result_table = RESULT_TABLE

    def __init__(self, threshold: float = DEFAULT_THRESHOLD, min_points: int = MIN_POINTS):
        self.threshold = threshold
        self.min_points = min_points

    def result_ddl(self) -> str:
        return f"""
            CREATE TABLE IF NOT EXISTS {RESULT_TABLE} (
                topic         VARCHAR NOT NULL,
                metric        VARCHAR NOT NULL,
                window_date   VARCHAR NOT NULL,
                value         DOUBLE,
                robust_z      DOUBLE,
                is_anomaly    BOOLEAN,
                n_points      INTEGER,
                series_median DOUBLE,
                series_mad    DOUBLE,
                threshold     DOUBLE,
                computed_at   VARCHAR,
                PRIMARY KEY (topic, metric, window_date)
            )
        """

    def compute(self, conn) -> List[Dict[str, Any]]:
        return compute_anomalies(conn, self.threshold, self.min_points)

    def store(self, conn, rows: List[Dict[str, Any]]) -> None:
        for r in rows:
            conn.execute(
                f"""
                INSERT OR REPLACE INTO {RESULT_TABLE}
                    (topic, metric, window_date, value, robust_z, is_anomaly,
                     n_points, series_median, series_mad, threshold, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    r["topic"], r["metric"], r["window_date"], r["value"],
                    r["robust_z"], r["is_anomaly"], r["n_points"],
                    r["series_median"], r["series_mad"], r["threshold"], r["computed_at"],
                ],
            )

    def params(self) -> Dict[str, Any]:
        return {"threshold": self.threshold, "min_points": self.min_points}

    def summary(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        flagged = sum(1 for r in rows if r["is_anomaly"])
        return {"flagged_windows": flagged, "topics": len({r["topic"] for r in rows})}


def _row_to_window(row: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a result row for the panel: observed value + expected band."""
    scale = _MAD_SCALE * (row.get("series_mad") or 0.0)
    thresh = row.get("threshold") or DEFAULT_THRESHOLD
    med = row.get("series_median") or 0.0
    band = interval(med, med - thresh * scale, med + thresh * scale, level=0.99)
    return {
        "window_date": row["window_date"],
        "metric": row["metric"],
        "value": row["value"],
        "robust_z": row["robust_z"],
        "is_anomaly": bool(row["is_anomaly"]),
        "expected_band": band,
    }


def detect_anomalies_payload(
    conn,
    topic: Optional[str] = None,
    metric: Optional[str] = None,
    only_flagged: bool = False,
) -> Dict[str, Any]:
    """The ``detect_anomalies`` tool payload: read precomputed anomalies (or
    compute on-demand for a single topic when nothing is stored), wrapped in
    the honesty envelope. Never ships a window without its expected band."""
    clauses, params = [], []
    if topic:
        clauses.append("topic = ?")
        params.append(topic)
    if metric:
        clauses.append("metric = ?")
        params.append(metric)
    where = " AND ".join(clauses) if clauses else None

    rows: List[Dict[str, Any]] = []
    try:
        rows = read_results(
            conn, RESULT_TABLE, where=where, params=params, order_by="window_date"
        )
    except Exception:
        rows = []  # table not created yet -> on-demand path below

    if not rows:
        # Nothing precomputed: compute on-demand (cheap for a single topic).
        computed = compute_anomalies(conn, topic=topic)
        rows = [
            r
            for r in computed
            if (metric is None or r["metric"] == metric)
        ]

    windows = [_row_to_window(r) for r in rows if not only_flagged or r["is_anomaly"]]
    n_points = max((int(r.get("n_points") or 0) for r in rows), default=0)
    return analytic_envelope(
        n=n_points,
        method=METHOD,
        assumptions=ASSUMPTIONS,
        topic=topic,
        metric=metric,
        threshold=(rows[0].get("threshold") if rows else DEFAULT_THRESHOLD),
        flagged=sum(1 for r in rows if r.get("is_anomaly")),
        windows=windows,
    )
