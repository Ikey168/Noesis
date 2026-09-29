"""WX09 (#2172): published forecasts against recorded observations with hand-computed metrics."""

from __future__ import annotations

from fractions import Fraction

import pytest

from src.kb.weather_identity import WeatherStationIdentity
from src.kb.weather_store import WeatherError
from src.kb.weather_verification import ForecastVerification
from tests.unit.weather import fixture_builder as fb
from tests.unit.weather import harness as h

PERIOD = {"period_from": "2026-06-10T00:00:00Z", "period_to": "2026-06-10T23:00:00Z"}


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    h.acquire_all(conn)
    identity = WeatherStationIdentity(conn)
    candidate = next(
        c
        for c in identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)[
            "candidates"
        ]
        if (c["left"], c["right"]) == (f"dwd-mosmix:{fb.MOSMIX}", f"dwd:{fb.DWD}")
    )
    identity.review(
        h.NS,
        candidate["candidate_id"],
        "accept",
        "same airfield",
        principal_id=h.REVIEWER,
        scopes=h.REVIEWER_SCOPES,
    )
    return conn, ForecastVerification(conn)


def group(result, provider, bucket):
    return next(
        r
        for r in result["by_provider_and_lead"]
        if r["provider"] == provider and r["lead_bucket"] == bucket
    )


def test_mosmix_temperature_against_dwd_observations_as_known_before_the_revision(
    world,
):
    _, verification = world
    result = verification.verify(
        h.NS,
        station=f"dwd:{fb.DWD}",
        parameter="air_temperature",
        providers=["dwd-mosmix"],
        knowledge_cutoff="2026-06-20T00:00:00Z",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        thresholds=[
            {"parameter": "air_temperature", "op": "gte", "value": "19.0", "unit": "°C"}
        ],
        **PERIOD,
    )
    # Run B (09Z): leads 3 h and 4 h; errors 18.6-18.4 = 0.2 and 19.3-19.5 = -0.2.
    short = group(result, "dwd-mosmix", "0-6h")
    assert (short["n"], short["bias"], short["mae"], short["rmse"]) == (
        2,
        "0",
        "0.2",
        "0.2",
    )
    # Run A (03Z): leads 9 h and 10 h; errors -0.2 and -0.5: bias -0.35, MAE 0.35, RMSE sqrt(0.145).
    long = group(result, "dwd-mosmix", "6-12h")
    assert (long["n"], long["bias"], long["mae"], long["rmse"]) == (
        2,
        "-0.35",
        "0.35",
        "0.380789",
    )
    table = short["contingency"][0]
    assert (
        table["hits"],
        table["misses"],
        table["false_alarms"],
        table["correct_negatives"],
    ) == (1, 0, 0, 1)
    assert (table["hit_rate"], table["false_alarm_ratio"]) == ("1", "0")
    # Run C (12Z) was acquired at 13:30, after both valid times: excluded and counted.
    assert result["excluded"]["captured_after_valid_time"] == 2
    assert {p["match_rule"] for p in result["pairs"]} == {"equivalent-station"}
    assert {p["observation"]["station"] for p in result["pairs"]} == {f"dwd:{fb.DWD}"}
    assert (
        result["ranking"] is None
        and isinstance(result["n"], int)
        and result["definitions"]["rmse"]
    )


def test_the_historical_revision_changes_the_pairs_and_metrics(world):
    _, verification = world
    result = verification.verify(
        h.NS,
        station=f"dwd:{fb.DWD}",
        parameter="air_temperature",
        providers=["dwd-mosmix"],
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        **PERIOD,
    )
    short, long = (
        group(result, "dwd-mosmix", "0-6h"),
        group(result, "dwd-mosmix", "6-12h"),
    )
    assert (short["bias"], short["mae"], short["rmse"]) == (
        "-0.1",
        "0.1",
        "0.141421",
    )  # errors 0.0, -0.2
    assert (long["bias"], long["mae"], long["rmse"]) == (
        "-0.45",
        "0.45",
        "0.452769",
    )  # errors -0.4, -0.5


def test_brier_score_for_published_precipitation_probabilities(world):
    _, verification = world
    result = verification.verify(
        h.NS,
        station=f"dwd:{fb.DWD}",
        parameter="precipitation_1h",
        providers=["dwd-mosmix"],
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        **PERIOD,
    )
    # Observed R1: 0.0 mm at 12Z (no event), 0.4 mm at 13Z (event > 0.1 mm).
    assert (
        group(result, "dwd-mosmix", "6-12h")["brier"] == "0.05"
    )  # ((0.10)^2 + (0.70-1)^2) / 2
    assert (
        group(result, "dwd-mosmix", "0-6h")["brier"] == "0.02125"
    )  # ((0.05)^2 + (0.80-1)^2) / 2
    amounts = group(result, "dwd-mosmix", "0-6h")
    assert (
        amounts["n_continuous"] == 2 and amounts["bias"] == "0.05"
    )  # RR1c 0.00/0.50 vs 0.0/0.4


def test_nws_against_the_declared_station_within_the_time_tolerance(world):
    _, verification = world
    result = verification.verify(
        h.NS,
        station=f"aviationweather:{fb.US_ICAO}",
        parameter="air_temperature",
        providers=["nws"],
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        **PERIOD,
    )
    errors = [
        Fraction(61 - 32) * 5 / 9 - Fraction("16.7"),
        Fraction(63 - 32) * 5 / 9 - Fraction("18.0"),
        Fraction(62 - 32) * 5 / 9 - Fraction("16.7"),
        Fraction(64 - 32) * 5 / 9 - Fraction("18.0"),
    ]
    # Forecast values are normalised to 6 decimals before differencing, exactly as the pairs record them.
    rounded = [Fraction(round(e * 10**6), 10**6) for e in errors]
    expected_bias = sum(rounded) / 4
    row = group(result, "nws", "0-6h")
    assert row["n"] == 4 and row["bias"] == format(float(expected_bias), ".6f").rstrip(
        "0"
    )
    assert {p["match_rule"] for p in result["pairs"]} == {"declared-grid-point"}
    assert {p["time_offset_s"] for p in result["pairs"]} == {-360}


def test_suspect_observations_are_excluded_and_counted(world):
    _, verification = world
    result = verification.verify(
        h.NS,
        station=f"aviationweather:{fb.ICAO}",
        parameter="air_temperature",
        providers=["dwd-mosmix"],
        tolerance_s=1800,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        record=False,
        **PERIOD,
    )
    assert (
        result["excluded"]["qc_failed_or_suspect"] >= 1
    )  # the METAR ending in '$' near 12Z


def test_runs_are_recorded_with_a_receipt_and_replay(world):
    _, verification = world
    result = verification.verify(
        h.NS,
        station=f"dwd:{fb.DWD}",
        parameter="air_temperature",
        providers=["dwd-mosmix"],
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        **PERIOD,
    )
    replay = verification.replay(h.NS, result["run_id"], scopes=h.SCOPES)
    assert replay["deterministic"] and replay["pinned_revisions"] == len(
        result["receipt"]["issuance_revisions"]
    ) + len(result["receipt"]["observation_revisions"])
    with pytest.raises(WeatherError):
        verification.verify(
            h.NS,
            station=f"dwd:{fb.DWD}",
            parameter="air_temperature",
            principal_id=h.PRINCIPAL,
            scopes=h.READ_ONLY,
            **PERIOD,
        )
