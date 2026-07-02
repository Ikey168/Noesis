"""
Dependency-light statistics for the analytics plane (R5 / Track DS).

Pure stdlib (``statistics`` / ``random`` / ``math``) implementations of the
techniques the first analytics need — robust z-scores, bootstrap confidence
intervals, and a permutation test over categorical splits. This is the
"heuristic-fallback house style" the plan mandates: no numpy/scipy required,
so the MCP tool servers stay import-safe and the analytics run anywhere.

Everything randomized takes an explicit ``seed`` so fits are reproducible
(and MLflow-loggable) and tests are deterministic.
"""

from __future__ import annotations

import math
import random
import statistics
from typing import Dict, List, Sequence, Tuple

# MAD-to-sigma scale for a normal distribution.
_MAD_SCALE = 1.4826
_EPS = 1e-9


def median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def mad(values: Sequence[float]) -> float:
    """Median absolute deviation about the median."""
    if not values:
        return 0.0
    med = median(values)
    return median([abs(v - med) for v in values])


def robust_z_scores(values: Sequence[float]) -> List[float]:
    """Robust z-scores using median and MAD (outlier-resistant).

    Falls back to the standard-deviation z-score when the MAD is zero (a
    series with a heavy mode), and to all-zeros when there is no spread at
    all — never divides by zero.
    """
    if not values:
        return []
    med = median(values)
    scale = _MAD_SCALE * mad(values)
    if scale <= _EPS:
        sd = statistics.pstdev(values) if len(values) > 1 else 0.0
        if sd <= _EPS:
            return [0.0 for _ in values]
        return [(v - med) / sd for v in values]
    return [(v - med) / scale for v in values]


def bootstrap_ci(
    values: Sequence[float],
    level: float = 0.95,
    resamples: int = 1000,
    seed: int = 0,
    statistic=statistics.mean,
) -> Tuple[float, float, float, int]:
    """Percentile bootstrap CI for a statistic (default the mean).

    Returns ``(point, lo, hi, resamples_used)``. With fewer than two values
    the interval collapses to the point estimate (no spread to resample).
    """
    values = [float(v) for v in values]
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0, 0
    point = float(statistic(values))
    if n == 1:
        return point, point, point, 0
    rng = random.Random(seed)
    stats_samples = []
    for _ in range(resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        stats_samples.append(float(statistic(sample)))
    stats_samples.sort()
    alpha = (1.0 - level) / 2.0
    lo = _percentile(stats_samples, alpha)
    hi = _percentile(stats_samples, 1.0 - alpha)
    return point, lo, hi, resamples


def _percentile(sorted_values: List[float], q: float) -> float:
    """Linear-interpolation percentile of an already-sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = q * (len(sorted_values) - 1)
    lo_i = int(math.floor(idx))
    hi_i = int(math.ceil(idx))
    if lo_i == hi_i:
        return sorted_values[lo_i]
    frac = idx - lo_i
    return sorted_values[lo_i] * (1 - frac) + sorted_values[hi_i] * frac


def _counts(labels: Sequence[str], categories: Sequence[str]) -> List[int]:
    index = {c: i for i, c in enumerate(categories)}
    out = [0] * len(categories)
    for label in labels:
        if label in index:
            out[index[label]] += 1
    return out


def chi_square_statistic(counts_a: Sequence[int], counts_b: Sequence[int]) -> float:
    """Pearson chi-square statistic of a 2xK contingency table."""
    total = sum(counts_a) + sum(counts_b)
    if total == 0:
        return 0.0
    row_totals = [sum(counts_a), sum(counts_b)]
    stat = 0.0
    for j in range(len(counts_a)):
        col_total = counts_a[j] + counts_b[j]
        for row_total, obs in ((row_totals[0], counts_a[j]), (row_totals[1], counts_b[j])):
            expected = row_total * col_total / total
            if expected > _EPS:
                stat += (obs - expected) ** 2 / expected
    return stat


def total_variation(counts_a: Sequence[int], counts_b: Sequence[int]) -> float:
    """Total-variation distance between two categorical distributions (0..1)."""
    na, nb = sum(counts_a), sum(counts_b)
    if na == 0 or nb == 0:
        return 0.0
    return 0.5 * sum(abs(a / na - b / nb) for a, b in zip(counts_a, counts_b))


def permutation_test_split(
    labels_a: Sequence[str],
    labels_b: Sequence[str],
    resamples: int = 2000,
    seed: int = 0,
) -> Dict[str, object]:
    """Permutation test for whether two groups' categorical splits differ.

    Shuffles the group labels across the pooled observations and counts how
    often the chi-square statistic meets or exceeds the observed value — an
    empirical p-value that needs no distributional assumptions. Reports the
    total-variation distance as an interpretable effect size.
    """
    categories = sorted(set(labels_a) | set(labels_b))
    counts_a = _counts(labels_a, categories)
    counts_b = _counts(labels_b, categories)
    observed = chi_square_statistic(counts_a, counts_b)
    tv = total_variation(counts_a, counts_b)

    pooled = list(labels_a) + list(labels_b)
    na = len(labels_a)
    n = len(pooled)
    p_value = 1.0
    if 0 < na < n and observed > _EPS:
        rng = random.Random(seed)
        ge = 0
        for _ in range(resamples):
            rng.shuffle(pooled)
            perm_stat = chi_square_statistic(
                _counts(pooled[:na], categories), _counts(pooled[na:], categories)
            )
            if perm_stat >= observed - _EPS:
                ge += 1
        # +1 smoothing keeps the p-value strictly positive.
        p_value = (ge + 1) / (resamples + 1)
    return {
        "categories": categories,
        "counts_a": counts_a,
        "counts_b": counts_b,
        "chi_square": observed,
        "tv_distance": tv,
        "p_value": p_value,
        "n": n,
        "resamples": resamples if (0 < na < n and observed > _EPS) else 0,
    }


# ---------------------------------------------------------------------------
# Time series (R6): cross-correlation lead-lag + exponential-smoothing forecast.
# ---------------------------------------------------------------------------


def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    """Pearson correlation of two equal-length series; 0 when undefined."""
    n = len(x)
    if n < 2 or n != len(y):
        return 0.0
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    denom = math.sqrt(sxx * syy)
    return sxy / denom if denom > _EPS else 0.0


def cross_correlation_lag(
    x: Sequence[float], y: Sequence[float], max_lag: int
) -> Dict[str, float]:
    """Best lead-lag between series x and y by cross-correlation.

    Positive ``lag`` means x leads y (x's past predicts y's present): shifting
    x forward by ``lag`` maximizes the correlation. Returns the best lag, its
    correlation, and the overlap length it was measured on.
    """
    x = [float(v) for v in x]
    y = [float(v) for v in y]
    n = min(len(x), len(y))
    if n < 3:
        return {"lag": 0, "correlation": 0.0, "overlap": n}
    max_lag = max(0, min(max_lag, n - 2))
    best = {"lag": 0, "correlation": 0.0, "overlap": n}
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            a, b = x[: n - lag], y[lag:n]
        else:
            a, b = x[-lag:n], y[: n + lag]
        if len(a) < 3:
            continue
        corr = pearson(a, b)
        stronger = abs(corr) > abs(best["correlation"]) + _EPS
        # On a (near-)tie in correlation — common with periodic series, where
        # several lags correlate equally — prefer the smallest-magnitude lag,
        # the most parsimonious lead-lag explanation.
        tie_closer = (
            abs(abs(corr) - abs(best["correlation"])) <= _EPS
            and abs(lag) < abs(best["lag"])
        )
        if stronger or tie_closer:
            best = {"lag": lag, "correlation": corr, "overlap": len(a)}
    return best


def holt_forecast(
    series: Sequence[float],
    horizon: int,
    alpha: float = 0.5,
    beta: float = 0.3,
    z: float = 1.96,
) -> Dict[str, object]:
    """Holt linear-trend exponential smoothing with prediction intervals.

    Returns per-step point forecasts and a symmetric prediction band from the
    in-sample one-step residual spread (widening with the horizon). Forecasts
    are never returned without the band — the honesty rule for R6.
    """
    series = [float(v) for v in series]
    n = len(series)
    if n < 2 or horizon < 1:
        last = series[-1] if series else 0.0
        return {"points": [last] * max(0, horizon), "lo": [], "hi": [], "sigma": 0.0}

    level = series[0]
    trend = series[1] - series[0]
    residuals: List[float] = []
    for value in series[1:]:
        predicted = level + trend
        residuals.append(value - predicted)
        new_level = alpha * value + (1 - alpha) * (level + trend)
        trend = beta * (new_level - level) + (1 - beta) * trend
        level = new_level

    sigma = statistics.pstdev(residuals) if len(residuals) > 1 else 0.0
    points, lo, hi = [], [], []
    for h in range(1, horizon + 1):
        point = level + h * trend
        # Prediction interval widens with sqrt(h) (random-walk-of-errors).
        width = z * sigma * math.sqrt(h)
        points.append(point)
        lo.append(point - width)
        hi.append(point + width)
    return {"points": points, "lo": lo, "hi": hi, "sigma": sigma}


def cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Cosine similarity between two sparse term-weight vectors."""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na > _EPS and nb > _EPS else 0.0
