"""Coverage diagnostics for official-statistics harvests (#1661).

Live runs on 2026-09-24 found Eurostat ``prc_hicp_manr`` and ECB ``ICP``
ending at 2025-12 after the HICP COICOP-2018 transition while reporting a
plain ``available`` status; these cases pin the stale-series diagnostic.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from services.ingest.common.series_model import SeriesRecord
from src.ingestion.connectors.dataset.base import (
    DatasetConnector,
    RawSeries,
    SeriesRef,
    _period_lag,
)
from src.ingestion.connectors.dataset.sdmx import _seasonal_adjustment

ACQUIRED = datetime(2026, 9, 24, tzinfo=timezone.utc)
ACQUIRED_MS = int(ACQUIRED.timestamp() * 1000)


class _Fixed(DatasetConnector):
    provider = "fixture"

    def __init__(self, frequency, periods):
        self.frequency = frequency
        self.periods = periods

    def discover(self, query=None):
        yield SeriesRef(locator="fixture/series")

    def fetch(self, ref):
        return RawSeries(ref=ref, content="{}", fetched_at=ACQUIRED_MS)

    def parse(self, raw):
        return [
            SeriesRecord(
                series_id="fixture:series",
                provider="fixture",
                title="fixture",
                frequency=self.frequency,
                as_of=ACQUIRED_MS,
                observations=[{"period": period, "value": 1.0} for period in self.periods],
                metadata={"acquired_at_ms": ACQUIRED_MS},
            )
        ]


@pytest.mark.parametrize(
    ("period", "frequency", "expected"),
    [
        ("2025", "annual", 1),
        ("2026-Q2", "quarterly", 1),
        ("2025-Q4", "quarterly", 3),
        ("2025-12", "monthly", 9),
        ("2026-09-20", "daily", 4),
        ("2026-08-27", "weekly", 4),
        ("2026-13", "monthly", -4),
        ("not-a-period", "monthly", None),
        ("2026-Q1", "monthly", None),
    ],
)
def test_period_lag(period, frequency, expected):
    assert _period_lag(period, frequency, date(2026, 9, 24)) == expected


def test_discontinued_monthly_series_is_reported_stale_not_available():
    report = _Fixed("monthly", ["2025-11", "2025-12"]).harvest_with_report()

    assert report["status"] == "partial"
    assert report["records"]
    assert report["diagnostics"] == [
        {
            "stage": "coverage",
            "locator": "fixture/series",
            "code": "stale_series",
            "series_id": "fixture:series",
            "last_period": "2025-12",
            "lag_periods": 9,
            "allowed_lag_periods": 4,
        }
    ]


@pytest.mark.parametrize(
    ("frequency", "periods"),
    [
        ("monthly", ["2026-06", "2026-07"]),
        ("quarterly", ["2026-Q1", "2026-Q2"]),
        ("annual", ["2024", "2025"]),
        ("daily", ["2026-09-23"]),
    ],
)
def test_normal_publication_lag_is_available(frequency, periods):
    assert _Fixed(frequency, periods).harvest_with_report()["status"] == "available"


def test_intentionally_historical_snapshot_is_not_reported_as_stale():
    connector = _Fixed("monthly", ["2020-01", "2020-02"])
    original_parse = connector.parse

    def parse(raw):
        records = original_parse(raw)
        records[0].metadata["coverage_freshness_applicable"] = False
        return records

    connector.parse = parse
    report = connector.harvest_with_report()
    assert report["status"] == "available"
    assert report["diagnostics"] == []


def test_ecb_adjustment_codes_map_to_seasonal_adjustment():
    assert _seasonal_adjustment({"ADJUSTMENT": "N"}, {}) == "not_adjusted"
    assert _seasonal_adjustment({"ADJUSTMENT": "Y"}, {}) == "adjusted"
    assert _seasonal_adjustment({"ADJUSTMENT": "S"}, {}) == "adjusted"
    # Working-day-only adjustment is not a seasonal-adjustment statement.
    assert _seasonal_adjustment({"ADJUSTMENT": "W"}, {}) == "unknown"
    assert _seasonal_adjustment({"CURRENCY": "USD"}, {}) == "unknown"
