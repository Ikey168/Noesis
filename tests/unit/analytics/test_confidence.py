"""Unit tests for confidence + significance (src/analytics/confidence.py)."""

from src.analytics.confidence import (
    score_confidence_payload,
    stance_significance_payload,
)
from src.analytics.honesty import validate_analytic_output



# ---------------------------------------------------------------------------
# score_confidence
# ---------------------------------------------------------------------------


def test_score_confidence_returns_interval(seed, conn):
    seed.outlet_scores(
        conn,
        [
            ("Reuters", "2025-06-02", 0.70, 0.7, 0.8, 0.6),
            ("Reuters", "2025-06-09", 0.74, 0.7, 0.82, 0.66),
            ("Reuters", "2025-06-16", 0.77, 0.71, 0.82, 0.78),
            ("Reuters", "2025-06-23", 0.73, 0.68, 0.79, 0.72),
        ],
    )
    payload = score_confidence_payload(conn, "Reuters", resamples=500)
    assert validate_analytic_output(payload, interval_fields=["composite"]) == []
    ci = payload["composite"]
    assert ci["lo"] <= ci["value"] <= ci["hi"]
    assert payload["n"] == 4
    assert "frame_diversity" in payload["components"]


def test_score_confidence_missing_outlet(seed, conn):
    seed.outlet_scores(conn, [])
    payload = score_confidence_payload(conn, "Nobody")
    assert payload["n"] == 0
    assert "note" in payload
    # Still honesty-valid (no interval claimed when there is no data).
    assert validate_analytic_output(payload) == []


def test_score_confidence_single_snapshot_collapses(seed, conn):
    seed.outlet_scores(conn, [("Solo", "2025-06-23", 0.6, 0.6, 0.6, 0.6)])
    payload = score_confidence_payload(conn, "Solo")
    ci = payload["composite"]
    assert ci["lo"] == ci["value"] == ci["hi"] == 0.6  # n=1 -> point, honestly
    assert payload["n"] == 1


# ---------------------------------------------------------------------------
# stance_significance
# ---------------------------------------------------------------------------


def test_stance_significance_detects_difference(seed, conn):
    seed.source_stances(
        conn,
        [
            ("OutletA", "energy", "supportive", 18),
            ("OutletA", "energy", "critical", 2),
            ("OutletB", "energy", "critical", 19),
            ("OutletB", "energy", "supportive", 3),
        ],
    )
    payload = stance_significance_payload(conn, "OutletA", "OutletB", "energy", resamples=500)
    assert validate_analytic_output(payload, interval_fields=["divergence"]) == []
    assert payload["significant"] is True
    assert payload["p_value"] < 0.05
    div = payload["divergence"]
    assert div["lo"] <= div["value"] <= div["hi"]
    assert payload["n"] == 42


def test_stance_significance_insufficient_data(seed, conn):
    seed.source_stances(conn, [("OutletA", "energy", "supportive", 5)])
    payload = stance_significance_payload(conn, "OutletA", "OutletB", "energy")
    assert "note" in payload
    assert validate_analytic_output(payload) == []


def test_stance_significance_similar_outlets_not_significant(seed, conn):
    seed.source_stances(
        conn,
        [
            ("OutletA", "energy", "supportive", 10),
            ("OutletA", "energy", "critical", 10),
            ("OutletB", "energy", "supportive", 10),
            ("OutletB", "energy", "critical", 10),
        ],
    )
    payload = stance_significance_payload(conn, "OutletA", "OutletB", "energy", resamples=300)
    assert payload["significant"] is False
    assert payload["divergence"]["value"] == 0.0
