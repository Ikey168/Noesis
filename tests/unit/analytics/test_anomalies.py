"""Unit tests for anomaly detection (src/analytics/anomalies.py)."""

from src.analytics.anomalies import (
    RESULT_TABLE,
    AnomalyJob,
    compute_anomalies,
    detect_anomalies_payload,
)
from src.analytics.framework import read_results, run_job
from src.analytics.honesty import validate_analytic_output


def _spike_series():
    # climate: five quiet days (3 articles) then a volume spike (30 articles).
    rows = []
    for day, n in [
        ("2025-06-01", 3),
        ("2025-06-02", 4),
        ("2025-06-03", 2),
        ("2025-06-04", 4),
        ("2025-06-05", 3),
        ("2025-06-06", 30),
    ]:
        rows += [("climate", day, 0.1)] * n
    return rows


def test_compute_flags_volume_spike(seed, conn):
    seed.news(conn, _spike_series())
    rows = compute_anomalies(conn, threshold=3.0)
    spike = [
        r
        for r in rows
        if r["metric"] == "volume" and r["window_date"] == "2025-06-06"
    ]
    assert spike and spike[0]["is_anomaly"] is True
    assert spike[0]["robust_z"] > 3.0
    # Quiet days are not flagged.
    quiet = [r for r in rows if r["metric"] == "volume" and r["window_date"] == "2025-06-01"]
    assert quiet[0]["is_anomaly"] is False


def test_sparse_topic_is_skipped(seed, conn):
    seed.news(conn, [("niche", "2025-06-01", 0.1), ("niche", "2025-06-02", 0.2)])
    assert compute_anomalies(conn) == []  # < MIN_POINTS days


def test_job_runs_end_to_end_through_framework(seed, conn, lock):
    seed.news(conn, _spike_series())
    result = run_job(AnomalyJob(threshold=3.0), conn=conn, lock=lock, log_mlflow=False)
    assert result["job"] == "detect_anomalies"
    assert result["flagged_windows"] >= 1
    stored = read_results(conn, RESULT_TABLE, where="is_anomaly = TRUE")
    assert stored  # flagged rows persisted to the result table


def test_payload_reads_precomputed_and_is_honesty_valid(seed, conn, lock):
    seed.news(conn, _spike_series())
    run_job(AnomalyJob(threshold=3.0), conn=conn, lock=lock, log_mlflow=False)
    payload = detect_anomalies_payload(conn, topic="climate", metric="volume")
    # Honesty contract holds.
    assert validate_analytic_output(payload) == []
    assert payload["method"].startswith("robust z-score")
    assert payload["n"] >= 5
    # Every window carries its expected band (no naked value).
    assert payload["windows"]
    for w in payload["windows"]:
        assert set(w["expected_band"]) == {"value", "lo", "hi", "level"}


def test_payload_computes_on_demand_without_result_table(seed, conn):
    seed.news(conn, _spike_series())
    # No fit run, so analytics_anomalies does not exist: on-demand path.
    payload = detect_anomalies_payload(conn, topic="climate", metric="volume")
    assert validate_analytic_output(payload) == []
    assert any(w["is_anomaly"] for w in payload["windows"])


def test_payload_only_flagged_filter(seed, conn, lock):
    seed.news(conn, _spike_series())
    run_job(AnomalyJob(threshold=3.0), conn=conn, lock=lock, log_mlflow=False)
    payload = detect_anomalies_payload(conn, topic="climate", only_flagged=True)
    assert payload["windows"]
    assert all(w["is_anomaly"] for w in payload["windows"])
