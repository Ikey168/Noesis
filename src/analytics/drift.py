"""
Semantic drift + topic forecasting (R6 / #602, Wave 1 caboose).

* ``semantic_drift(term, window)`` measures how a term's *meaning* shifts by
  comparing its lexical context (co-occurring words) between an early and a
  late half of the window, with a bootstrap interval on the drift magnitude.
  The plan's target is embedding drift; the dependency-light fallback is the
  bag-of-words context vector.
* ``forecast_topic(topic, horizon)`` projects a topic's daily coverage
  velocity with Holt exponential smoothing — and, per the honesty rule,
  **never** without a prediction interval.

Honesty envelope throughout; pure-stdlib maths.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.analytics.conformal import calibrated_envelope_fields, conformal_interval
from src.analytics.honesty import analytic_envelope, interval
from src.analytics.stats import cosine, holt_forecast
from src.analytics.text import context_counts, tokenize
from src.database.news_articles_compat import corpus_table

DRIFT_METHOD = "lexical context-vector cosine drift (embedding fallback)"
DRIFT_ASSUMPTIONS = [
    "drift is lexical co-occurrence shift, not an embedding-space distance",
    "early vs late halves of the window are compared",
    "needs the term to appear in several documents in each half",
]

FORECAST_METHOD = "Holt linear-trend exponential smoothing"
FORECAST_ASSUMPTIONS = [
    "assumes locally linear velocity; news is noisy so intervals are wide",
    "prediction interval from in-sample one-step residuals, widening with horizon",
    "a point forecast is never returned without its interval",
]


def _mentions(conn, term: str, days: int) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(2, days))
    rows = conn.execute(
        f"SELECT title, publish_date FROM {corpus_table(conn)} "
        "WHERE title ILIKE ? AND publish_date >= ? ORDER BY publish_date",
        [f"%{term}%", cutoff],
    ).fetchall()
    return [{"title": r[0] or "", "day": r[1]} for r in rows]


def _normalize(counts) -> Dict[str, float]:
    total = sum(counts.values())
    return {t: c / total for t, c in counts.items()} if total else {}


def semantic_drift_payload(conn, term: str, window: int = 90, seed: int = 0) -> Dict[str, Any]:
    """Lexical-context drift of a term between the early and late window halves."""
    if not term:
        return {"error": "semantic_drift requires a term"}
    docs = _mentions(conn, term, window)
    n = len(docs)
    if n < 4:
        return analytic_envelope(
            n=n, method=DRIFT_METHOD, assumptions=DRIFT_ASSUMPTIONS,
            term=term, note="too few mentions to measure drift",
        )
    mid = n // 2
    early = [d["title"] for d in docs[:mid]]
    late = [d["title"] for d in docs[mid:]]
    early_ctx = _normalize(context_counts(early, term))
    late_ctx = _normalize(context_counts(late, term))
    drift = 1.0 - cosine(early_ctx, late_ctx)

    # Bootstrap the drift by resampling documents within each half.
    rng = random.Random(seed)
    samples = []
    for _ in range(400):
        e = [early[rng.randrange(len(early))] for _ in range(len(early))]
        la = [late[rng.randrange(len(late))] for _ in range(len(late))]
        samples.append(
            1.0 - cosine(_normalize(context_counts(e, term)), _normalize(context_counts(la, term)))
        )
    samples.sort()
    lo = samples[int(0.025 * (len(samples) - 1))]
    hi = samples[int(0.975 * (len(samples) - 1))]

    rising = sorted(late_ctx, key=lambda t: late_ctx[t] - early_ctx.get(t, 0.0), reverse=True)[:6]
    falling = sorted(early_ctx, key=lambda t: early_ctx[t] - late_ctx.get(t, 0.0), reverse=True)[:6]
    return analytic_envelope(
        n=n, method=DRIFT_METHOD, assumptions=DRIFT_ASSUMPTIONS,
        term=term, window_days=window,
        drift=interval(drift, lo, hi, 0.95),
        rising_terms=rising, falling_terms=falling,
    )


def _velocity_series(conn, topic: str, days: int) -> List[float]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(3, days))
    rows = conn.execute(
        "SELECT CAST(publish_date AS DATE) AS day, COUNT(*) "
        f"FROM {corpus_table(conn)} WHERE category = ? AND publish_date >= ? "
        "GROUP BY 1 ORDER BY 1",
        [topic, cutoff],
    ).fetchall()
    return [float(r[1]) for r in rows]


def forecast_topic_payload(
    conn, topic: str, horizon: int = 7, window: int = 60
) -> Dict[str, Any]:
    """Holt forecast of a topic's daily coverage velocity, always with a band."""
    if not topic:
        return {"error": "forecast_topic requires a topic"}
    series = _velocity_series(conn, topic, window)
    if len(series) < 3:
        return analytic_envelope(
            n=len(series), method=FORECAST_METHOD, assumptions=FORECAST_ASSUMPTIONS,
            topic=topic, note="too little history to forecast",
        )
    horizon = max(1, min(horizon, 30))
    fc = holt_forecast(series, horizon)
    # M7.1: build a *calibrated* (split-conformal) band from the in-sample
    # one-step residuals instead of the asserted Gaussian z*sigma band, so the
    # 95% is measured, not claimed. The band widens with sqrt(step).
    residuals = fc.get("residuals") or []
    level = 0.95
    points = [
        {
            "step": i + 1,
            # Coverage volume can't go negative; clamp the calibrated band at zero.
            "forecast": _clamp_lo(
                conformal_interval(fc["points"][i], residuals, level, scale=math.sqrt(i + 1))
            ),
        }
        for i in range(horizon)
    ]
    calib = calibrated_envelope_fields(residuals, level)
    return analytic_envelope(
        n=len(series), method=FORECAST_METHOD, assumptions=FORECAST_ASSUMPTIONS,
        topic=topic, horizon=horizon, history=series[-14:],
        residual_sigma=round(float(fc["sigma"]), 4), points=points,
        coverage=calib["coverage"], level=calib["level"],
        calibration_n=calib["calibration_n"],
    )


def _clamp_lo(iv: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp a coverage-count interval at zero without breaking lo <= value <= hi."""
    return interval(max(0.0, iv["value"]), max(0.0, iv["lo"]), max(0.0, iv["hi"]), iv["level"])
