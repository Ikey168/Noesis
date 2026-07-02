"""
Confidence and significance for the transparency mission (R5 / #598).

Two tools that make the published transparency figures *defensible* rather
than bare point estimates:

* ``score_confidence(outlet)`` — a percentile-bootstrap confidence interval
  on an outlet's composite transparency score, from its weekly history, so
  the ranking panels can draw error bars instead of a single number.
* ``stance_significance(a, b, topic)`` — a permutation test (chi-square
  statistic) on whether two outlets' stance splits on a topic genuinely
  differ, with a bootstrap interval on the divergence effect size, so a
  stance comparison carries a significance badge.

Both wrap their output in the honesty envelope. Pure-stdlib maths.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Sequence

from src.analytics.honesty import analytic_envelope, interval
from src.analytics.stats import (
    bootstrap_ci,
    permutation_test_split,
    total_variation,
    _counts,
)

SIGNIFICANCE_ALPHA = 0.05

CONFIDENCE_METHOD = "percentile bootstrap CI on weekly composite scores"
CONFIDENCE_ASSUMPTIONS = [
    "weekly composite snapshots are treated as an exchangeable sample",
    "a single snapshot yields a point with no interval (n=1)",
    "reflects week-to-week variation, not within-week measurement error",
]

SIGNIFICANCE_METHOD = "permutation test (chi-square statistic) on stance splits"
SIGNIFICANCE_ASSUMPTIONS = [
    "documents are treated as independent draws per outlet",
    "empirical p-value from label permutations; no distributional assumption",
    "reconstructed from aggregated stance counts, not per-document labels",
]


# ---------------------------------------------------------------------------
# score_confidence
# ---------------------------------------------------------------------------


def score_confidence_payload(
    conn,
    outlet: str,
    level: float = 0.95,
    resamples: int = 2000,
    seed: int = 0,
) -> Dict[str, Any]:
    """Bootstrap CI on an outlet's composite transparency score."""
    rows = conn.execute(
        """
        SELECT composite_score, frame_diversity, attribution_rate, stance_neutrality
        FROM outlet_scores
        WHERE source = ? AND composite_score IS NOT NULL
        ORDER BY score_date
        """,
        [outlet],
    ).fetchall()
    composites = [float(r[0]) for r in rows]
    if not composites:
        return analytic_envelope(
            n=0,
            method=CONFIDENCE_METHOD,
            assumptions=CONFIDENCE_ASSUMPTIONS,
            outlet=outlet,
            note="no transparency scores recorded for this outlet",
        )
    point, lo, hi, used = bootstrap_ci(
        composites, level=level, resamples=resamples, seed=seed
    )
    components = {
        "frame_diversity": _mean([r[1] for r in rows]),
        "attribution_rate": _mean([r[2] for r in rows]),
        "stance_neutrality": _mean([r[3] for r in rows]),
    }
    return analytic_envelope(
        n=len(composites),
        method=CONFIDENCE_METHOD,
        assumptions=CONFIDENCE_ASSUMPTIONS,
        outlet=outlet,
        resamples=used,
        composite=interval(point, lo, hi, level),
        components=components,
    )


def _mean(values: Sequence[Any]) -> float:
    nums = [float(v) for v in values if v is not None]
    return sum(nums) / len(nums) if nums else 0.0


# ---------------------------------------------------------------------------
# stance_significance
# ---------------------------------------------------------------------------


def _stance_labels(conn, outlet: str, topic: str) -> List[str]:
    """Reconstruct per-document stance labels from aggregated counts."""
    rows = conn.execute(
        """
        SELECT stance, document_count
        FROM source_stances
        WHERE source = ? AND topic = ?
        """,
        [outlet, topic],
    ).fetchall()
    labels: List[str] = []
    for stance, count in rows:
        labels.extend([str(stance)] * int(count or 0))
    return labels


def _bootstrap_tv_ci(
    labels_a: Sequence[str],
    labels_b: Sequence[str],
    categories: Sequence[str],
    level: float,
    resamples: int,
    seed: int,
) -> tuple:
    """Percentile-bootstrap CI on the total-variation divergence."""
    na, nb = len(labels_a), len(labels_b)
    observed = total_variation(_counts(labels_a, categories), _counts(labels_b, categories))
    if na == 0 or nb == 0:
        return observed, observed, observed
    rng = random.Random(seed)
    samples = []
    for _ in range(resamples):
        sa = [labels_a[rng.randrange(na)] for _ in range(na)]
        sb = [labels_b[rng.randrange(nb)] for _ in range(nb)]
        samples.append(total_variation(_counts(sa, categories), _counts(sb, categories)))
    samples.sort()
    alpha = (1.0 - level) / 2.0
    lo = samples[int(alpha * (len(samples) - 1))]
    hi = samples[int((1.0 - alpha) * (len(samples) - 1))]
    return observed, lo, hi


def stance_significance_payload(
    conn,
    a: str,
    b: str,
    topic: str,
    level: float = 0.95,
    resamples: int = 2000,
    seed: int = 0,
) -> Dict[str, Any]:
    """Permutation test on whether two outlets' stance splits on a topic differ."""
    labels_a = _stance_labels(conn, a, topic)
    labels_b = _stance_labels(conn, b, topic)
    if not labels_a or not labels_b:
        return analytic_envelope(
            n=len(labels_a) + len(labels_b),
            method=SIGNIFICANCE_METHOD,
            assumptions=SIGNIFICANCE_ASSUMPTIONS,
            topic=topic,
            outlet_a=a,
            outlet_b=b,
            note="insufficient stance data for one or both outlets on this topic",
        )
    test = permutation_test_split(labels_a, labels_b, resamples=resamples, seed=seed)
    tv, tv_lo, tv_hi = _bootstrap_tv_ci(
        labels_a, labels_b, test["categories"], level, resamples, seed + 1
    )
    p_value = float(test["p_value"])
    return analytic_envelope(
        n=int(test["n"]),
        method=SIGNIFICANCE_METHOD,
        assumptions=SIGNIFICANCE_ASSUMPTIONS,
        topic=topic,
        outlet_a=a,
        outlet_b=b,
        categories=test["categories"],
        counts_a=test["counts_a"],
        counts_b=test["counts_b"],
        chi_square=float(test["chi_square"]),
        p_value=p_value,
        significant=bool(p_value < SIGNIFICANCE_ALPHA),
        alpha=SIGNIFICANCE_ALPHA,
        divergence=interval(tv, tv_lo, tv_hi, level),
    )
