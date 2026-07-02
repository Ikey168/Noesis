"""Unit tests for the R6 time-series primitives (src/analytics/stats.py)."""

from src.analytics.stats import cosine, cross_correlation_lag, holt_forecast, pearson


def test_pearson_perfect_and_undefined():
    assert pearson([1, 2, 3], [2, 4, 6]) == 1.0
    assert pearson([1, 1, 1], [1, 2, 3]) == 0.0  # zero variance
    assert pearson([1], [1]) == 0.0


def test_cross_correlation_finds_positive_lag():
    # x leads y by 2 steps.
    x = [1, 5, 1, 1, 5, 1, 1, 5, 1, 1]
    y = [1, 1, 1, 5, 1, 1, 5, 1, 1, 5]
    res = cross_correlation_lag(x, y, 4)
    assert res["lag"] == 2
    assert res["correlation"] > 0.9


def test_cross_correlation_symmetric_negative_lag():
    x = [1, 1, 1, 5, 1, 1, 5, 1]
    y = [1, 5, 1, 1, 5, 1, 1, 1]
    res = cross_correlation_lag(x, y, 4)
    assert res["lag"] < 0  # x lags y


def test_cross_correlation_too_short():
    assert cross_correlation_lag([1, 2], [1, 2], 3)["lag"] == 0


def test_holt_forecast_extends_trend_with_widening_band():
    fc = holt_forecast([1, 2, 3, 4, 5, 6], horizon=3)
    assert fc["points"][0] > 6  # continues the upward trend
    assert len(fc["points"]) == len(fc["lo"]) == len(fc["hi"]) == 3
    # Points bracketed by the band; band widens with the horizon.
    for i in range(3):
        assert fc["lo"][i] <= fc["points"][i] <= fc["hi"][i]
    assert (fc["hi"][2] - fc["lo"][2]) >= (fc["hi"][0] - fc["lo"][0])


def test_holt_forecast_noisy_series_has_nonzero_band():
    fc = holt_forecast([5, 8, 4, 9, 3, 10, 6, 7], horizon=2)
    assert fc["sigma"] > 0
    assert fc["hi"][0] > fc["lo"][0]


def test_holt_forecast_edge_cases():
    assert holt_forecast([5], 3)["points"] == [5, 5, 5]
    assert holt_forecast([1, 2, 3], 0)["points"] == []


def test_cosine_similarity():
    assert cosine({"a": 1.0}, {"a": 1.0}) == 1.0
    assert cosine({"a": 1.0}, {"b": 1.0}) == 0.0
    assert cosine({}, {"a": 1.0}) == 0.0
