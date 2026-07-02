"""Unit tests for the pure-stdlib statistics (src/analytics/stats.py)."""

import pytest

from src.analytics.stats import (
    bootstrap_ci,
    chi_square_statistic,
    mad,
    median,
    permutation_test_split,
    robust_z_scores,
    total_variation,
)


def test_median_and_mad():
    assert median([1, 2, 3, 4, 5]) == 3
    assert median([]) == 0.0
    assert mad([1, 1, 1]) == 0.0
    assert mad([1, 2, 3, 4, 5]) == 1.0


def test_robust_z_flags_outlier():
    # A series with real day-to-day variation (nonzero MAD) plus one spike.
    z = robust_z_scores([3, 4, 2, 3, 4, 3, 20])
    assert z[-1] > 3.0  # the 20 is a strong outlier
    assert all(abs(v) < 3.0 for v in z[:6])


def test_robust_z_zero_spread_is_all_zero():
    assert robust_z_scores([5, 5, 5, 5]) == [0.0, 0.0, 0.0, 0.0]
    assert robust_z_scores([]) == []


def test_robust_z_falls_back_to_std_when_mad_zero():
    # Heavy mode -> MAD 0, but there is real spread; std-based z is used.
    z = robust_z_scores([5, 5, 5, 5, 5, 5, 5, 100])
    assert z[-1] > 2.0


def test_bootstrap_ci_is_deterministic_and_brackets_point():
    point, lo, hi, used = bootstrap_ci([1, 2, 3, 4, 5], seed=7, resamples=500)
    assert lo <= point <= hi
    assert used == 500
    # Deterministic with the same seed.
    assert bootstrap_ci([1, 2, 3, 4, 5], seed=7, resamples=500) == (point, lo, hi, used)


def test_bootstrap_ci_edge_cases():
    assert bootstrap_ci([]) == (0.0, 0.0, 0.0, 0)
    point, lo, hi, used = bootstrap_ci([0.5])
    assert (point, lo, hi, used) == (0.5, 0.5, 0.5, 0)


def test_chi_square_and_tv():
    assert chi_square_statistic([0, 0], [0, 0]) == 0.0
    # Identical distributions -> zero divergence.
    assert total_variation([5, 5], [5, 5]) == 0.0
    # Opposite distributions -> maximal TV distance.
    assert total_variation([10, 0], [0, 10]) == pytest.approx(1.0)


def test_permutation_test_detects_difference():
    # Strongly different splits -> small p-value, large TV.
    a = ["supportive"] * 20 + ["critical"] * 2
    b = ["critical"] * 20 + ["supportive"] * 2
    result = permutation_test_split(a, b, resamples=500, seed=1)
    assert result["p_value"] < 0.05
    assert result["tv_distance"] > 0.5
    assert result["n"] == len(a) + len(b)


def test_permutation_test_identical_groups():
    a = ["supportive"] * 10 + ["critical"] * 10
    b = ["supportive"] * 10 + ["critical"] * 10
    result = permutation_test_split(a, b, resamples=300, seed=1)
    assert result["tv_distance"] == 0.0
    assert result["p_value"] == 1.0  # no observed difference
    assert result["resamples"] == 0
