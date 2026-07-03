"""M7.1: calibrated (conformal) intervals replace asserted Gaussian bands, with
a documented coverage rate. Covers the conformal primitives, the coverage
guarantee, and the forecast analytic carrying a measured coverage."""

from datetime import datetime, timedelta, timezone

import pytest

from src.analytics.conformal import (
    calibrated_envelope_fields,
    calibration_coverage,
    conformal_interval,
    conformal_quantile,
    empirical_coverage,
)
from src.analytics.honesty import is_interval, validate_analytic_output

duckdb = pytest.importorskip("duckdb")


def test_conformal_quantile_finite_sample_correction():
    residuals = [1.0, -2.0, 3.0, -4.0]  # |.| = 1,2,3,4
    # ceil((4+1)*0.95)=5 -> clamped to n=4 -> the max, 4.
    assert conformal_quantile(residuals, 0.95) == 4.0
    # ceil((4+1)*0.5)=3 -> the 3rd smallest |residual| (conservative), 3.0.
    assert conformal_quantile(residuals, 0.5) == 3.0
    # A much lower level takes a smaller quantile.
    assert conformal_quantile(residuals, 0.1) == 1.0
    assert conformal_quantile([], 0.95) == 0.0


def test_calibration_coverage_is_at_least_the_level():
    residuals = [0.1 * i - 1.0 for i in range(50)]
    for level in (0.5, 0.8, 0.95):
        assert calibration_coverage(residuals, level) >= level


def test_conformal_interval_is_well_formed():
    iv = conformal_interval(10.0, [1.0, -1.5, 2.0, -0.5], level=0.9)
    assert is_interval(iv) and iv["level"] == 0.9
    assert iv["lo"] <= 10.0 <= iv["hi"]


def test_coverage_guarantee_on_held_out_data():
    # Calibrate the band on one residual sample, measure coverage on a held-out
    # sample from the same distribution: it should be near the target level.
    import random

    rng = random.Random(7)
    calib = [rng.gauss(0, 1) for _ in range(500)]
    q = conformal_quantile(calib, 0.9)
    truths = [rng.gauss(0, 1) for _ in range(2000)]
    intervals = [{"lo": -q, "hi": q, "value": 0.0, "level": 0.9} for _ in truths]
    cov = empirical_coverage(intervals, truths)
    assert 0.86 <= cov <= 0.94  # ~0.90, within sampling tolerance


def test_calibrated_envelope_fields_document_coverage():
    fields = calibrated_envelope_fields([1.0, -1.0, 0.5, -0.5], 0.95)
    assert fields["level"] == 0.95
    assert fields["coverage"] >= 0.95
    assert fields["calibration_n"] == 4


def _warehouse(tmp_path):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, publish_date TIMESTAMP, "
        "source VARCHAR, category VARCHAR)"
    )
    now = datetime.now(timezone.utc)
    rows = []
    k = 0
    for day in range(40):
        d = now - timedelta(days=40 - day)
        for _ in range(3 + (day % 4)):  # a varying daily count
            rows.append((f"a{k}", "t", d, "Wire", "energy"))
            k += 1
    con.executemany(
        "INSERT INTO news_articles (id, title, publish_date, source, category) VALUES (?,?,?,?,?)",
        rows,
    )
    return con


def test_forecast_payload_carries_a_calibrated_band(tmp_path):
    from src.analytics.drift import forecast_topic_payload

    con = _warehouse(tmp_path)
    out = forecast_topic_payload(con, "energy", horizon=5)
    con.close()
    assert "error" not in out
    # A documented, measured coverage accompanies the forecast, not just a level.
    assert out["coverage"] >= out["level"] >= 0.9
    assert out["calibration_n"] > 0
    # Every point's band is a well-formed interval (calibrated, clamped >= 0).
    for p in out["points"]:
        assert is_interval(p["forecast"]) and p["forecast"]["lo"] >= 0.0
    assert validate_analytic_output(out, interval_fields=()) == []
