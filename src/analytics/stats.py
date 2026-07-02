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
