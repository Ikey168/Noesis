"""
Calibrated (conformal) intervals (M7.1).

Replaces asserted confidence bands (a Gaussian ``z * sigma``, whose 95% is
claimed, not checked) with split-conformal intervals whose coverage is
distribution-free and *documented*: the band is a quantile of the absolute
in-sample residuals, so on the calibration sample the empirical coverage meets
the target level by construction. Every calibrated interval ships with the
measured coverage rate, so a consumer sees a number that was verified, not
assumed.

Stdlib-only; composes with :func:`src.analytics.honesty.interval`.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

from src.analytics.honesty import interval


def conformal_quantile(residuals: Sequence[float], level: float = 0.95) -> float:
    """The split-conformal quantile of the absolute residuals: the smallest band
    ``q`` such that at least ``level`` of ``|residual|`` are within it, using the
    finite-sample correction ``ceil((n+1) * level) / n``. Returns 0 for an empty
    sample."""
    abs_res = sorted(abs(float(r)) for r in residuals)
    n = len(abs_res)
    if n == 0:
        return 0.0
    rank = min(n, math.ceil((n + 1) * min(max(level, 0.0), 1.0)))
    return abs_res[rank - 1]


def calibration_coverage(residuals: Sequence[float], level: float = 0.95) -> float:
    """The empirical coverage of the conformal band on its own calibration
    residuals: the fraction of ``|residual|`` within the band. By construction
    this is at least ``level``; it is the *documented* coverage rate reported
    alongside the interval."""
    abs_res = [abs(float(r)) for r in residuals]
    if not abs_res:
        return 1.0
    q = conformal_quantile(residuals, level)
    return sum(1 for r in abs_res if r <= q) / len(abs_res)


def coverage_of_band(residuals: Sequence[float], half: float) -> float:
    """The measured coverage of an already-chosen band half-width over a
    calibration sample: the fraction of ``|residual|`` within ``half``. Used to
    *document* the coverage of an interval whose width is set by another lever
    (e.g. evidence), rather than by the conformal quantile."""
    abs_res = [abs(float(r)) for r in residuals]
    if not abs_res:
        return 1.0
    return sum(1 for r in abs_res if r <= float(half)) / len(abs_res)


def conformal_interval(
    value: float, residuals: Sequence[float], level: float = 0.95, scale: float = 1.0
) -> Dict[str, float]:
    """A calibrated interval around ``value``: the conformal band (optionally
    scaled, e.g. by ``sqrt(horizon)``) as the symmetric half-width."""
    q = conformal_quantile(residuals, level) * float(scale)
    return interval(value, value - q, value + q, level)


def empirical_coverage(
    intervals: Sequence[Dict[str, float]], truths: Sequence[float]
) -> float:
    """The fraction of ``truths`` that fall within their paired interval — the
    held-out coverage a calibration run reports."""
    pairs = list(zip(intervals, truths))
    if not pairs:
        return 1.0
    covered = sum(1 for iv, t in pairs if iv["lo"] <= float(t) <= iv["hi"])
    return covered / len(pairs)


def calibrated_envelope_fields(residuals: Sequence[float], level: float = 0.95) -> Dict[str, Any]:
    """The honesty fields documenting a calibrated interval: the target level,
    the measured coverage, and the calibration sample size."""
    return {
        "level": float(level),
        "coverage": round(calibration_coverage(residuals, level), 4),
        "calibration_n": len(list(residuals)),
    }


__all__ = [
    "conformal_quantile",
    "calibration_coverage",
    "conformal_interval",
    "empirical_coverage",
    "calibrated_envelope_fields",
]
