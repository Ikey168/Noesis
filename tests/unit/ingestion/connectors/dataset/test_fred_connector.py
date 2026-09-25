"""Unit tests for the FRED dataset connector (offline, injected HTTP)."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from services.ingest.common.series_model import SeriesRecord
from src.ingestion.connectors.dataset.fred import FredConnector

HEADER = {
    "seriess": [
        {
            "id": "UNRATE",
            "title": "Unemployment Rate",
            "frequency": "Monthly",
            "units": "Percent",
            "last_updated": "2025-01-10 07:31:02",
        }
    ]
}
OBS = {
    "observations": [
        {"date": "2024-10-01", "value": "4.1"},
        {"date": "2024-11-01", "value": "4.2"},
        {"date": "2024-12-01", "value": "."},  # FRED missing marker
    ]
}


def _connector(header=HEADER, obs=OBS, api_key="test-key"):
    calls = []

    def fake_get(url: str) -> str:
        calls.append(url)
        if "/series/observations" in url:
            return json.dumps(obs)
        return json.dumps(header)

    c = FredConnector(api_key=api_key, http_get=fake_get)
    c._calls = calls  # type: ignore[attr-defined]
    return c


def test_harvest_builds_monthly_series():
    conn = _connector()
    records = list(conn.harvest("UNRATE"))
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, SeriesRecord)
    assert rec.series_id == "fred:UNRATE"
    assert rec.provider == "fred"
    assert rec.frequency == "monthly"
    assert rec.unit == "percent"
    # Monthly dates normalize to YYYY-MM; the "." value becomes None.
    assert [(o.period, o.value) for o in rec.observations] == [
        ("2024-10", 4.1),
        ("2024-11", 4.2),
        ("2024-12", None),
    ]
    assert rec.license.startswith("FRED")
    assert "api_key" not in (rec.source_url or "")
    assert "test-key" not in (rec.source_url or "")
    assert rec.metadata["acquired_at_ms"] >= 0
    assert rec.metadata["provider_release_at_ms"] is None
    assert rec.metadata["seasonal_adjustment"] == "unknown"


def test_geography_spec_included_in_id():
    conn = _connector()
    records = list(conn.harvest({"series": "UNRATE", "geography": "US"}))
    assert records[0].series_id == "fred:UNRATE:US"
    assert records[0].geography == "US"


def test_historical_vintage_queries_alfred_period_and_keeps_acquisition_clock():
    header = {
        "seriess": [
            {
                **HEADER["seriess"][0],
                "realtime_start": "2020-03-01",
                "realtime_end": "2020-03-01",
                "seasonal_adjustment": "Seasonally Adjusted",
                "seasonal_adjustment_short": "SA",
            }
        ]
    }
    obs = {
        "observations": [
            {
                "date": "2020-02-01",
                "value": "3.5",
                "realtime_start": "2020-03-01",
                "realtime_end": "9999-12-31",
            }
        ]
    }
    conn = _connector(header=header, obs=obs)
    record = list(
        conn.harvest(
            {
                "series": "UNRATE",
                "release_id": 10,
                "vintage_date": "2020-03-01",
                "observation_start": "2019-01-01",
                "observation_end": "2020-12-31",
            }
        )
    )[0]

    urls = conn._calls  # type: ignore[attr-defined]
    header_params, observation_params = [
        parse_qs(urlsplit(url).query) for url in urls
    ]
    assert header_params["realtime_start"] == ["2020-03-01"]
    assert header_params["realtime_end"] == ["2020-03-01"]
    assert "observation_start" not in header_params
    assert observation_params["observation_start"] == ["2019-01-01"]
    assert record.as_of == 1583020800000
    assert record.metadata["vintage_id"] == "UNRATE@2020-03-01"
    assert record.metadata["vintage_basis"] == "explicit_alfred_realtime_period"
    assert record.metadata["acquired_at_ms"] > record.as_of
    assert record.metadata["provider_release_id"] == "10"
    assert record.metadata["provider_release_id_provenance"] == "caller_supplied"
    assert record.metadata["seasonal_adjustment"] == "adjusted"
    assert record.metadata["coverage_freshness_applicable"] is False
    assert record.metadata["observation_realtime_periods"]["2020-02-01"] == {
        "start": "2020-03-01",
        "end": "9999-12-31",
    }

    report = _connector(header=header, obs=obs).harvest_with_report(
        {
            "series": "UNRATE",
            "vintage_date": "2020-03-01",
            "observation_end": "2020-02-01",
        }
    )
    assert report["status"] == "available"
    assert report["diagnostics"] == []


def test_release_calendar_preserves_day_precision_and_exposes_coverage_limits():
    calls = []
    payload = {
        "count": 2,
        "release_dates": [
            {"release_id": 10, "date": "2020-03-01"},
            {"release_id": 10, "date": "2020-04-01"},
        ],
    }
    conn = FredConnector(
        api_key="calendar-secret",
        http_get=lambda url: calls.append(url) or json.dumps(payload),
    )
    calendar = conn.release_dates(
        10,
        realtime_start="2020-01-01",
        realtime_end="2020-12-31",
        include_release_dates_with_no_data=True,
    )
    params = parse_qs(urlsplit(calls[0]).query)
    assert params["release_id"] == ["10"]
    assert params["include_release_dates_with_no_data"] == ["true"]
    assert calendar["coverage"] == "provider_returned_dates"
    assert calendar["release_time_precision"] == "date_only"
    assert calendar["dates"][0] == {
        "date": "2020-03-01",
        "date_ms": 1583020800000,
        "precision": "day",
        "source_release_id": "10",
    }
    assert "calendar-secret" not in repr(calendar)

    assert FredConnector(api_key="").harvest_with_report("UNRATE")["status"] == "blocked"
    with pytest.raises(ValueError, match="requires FRED_API_KEY"):
        FredConnector(api_key="").release_dates(10)


def test_series_release_lookup_identifies_release_ids_for_calendar_requests():
    calls = []
    connector = FredConnector(
        api_key="series-secret",
        http_get=lambda url: calls.append(url)
        or json.dumps(
            {
                "releases": [
                    {
                        "id": 21,
                        "name": "H.6 Money Stock Measures",
                        "press_release": True,
                        "link": "https://example.invalid/h6",
                        "realtime_start": "2020-03-01",
                        "realtime_end": "2020-03-01",
                    }
                ]
            }
        ),
    )
    result = connector.series_releases("M1SL")
    params = parse_qs(urlsplit(calls[0]).query)
    assert params["series_id"] == ["M1SL"]
    assert result["releases"][0]["release_id"] == "21"
    assert result["coverage"] == "provider_returned_releases"
    assert "series-secret" not in repr(result)


def test_not_configured_yields_nothing():
    # No key -> skip-with-warning, harvest returns nothing (does not raise).
    conn = FredConnector(api_key="", http_get=lambda url: "{}")
    assert conn.configured is False
    assert list(conn.harvest("UNRATE")) == []


def test_readiness_report_exposes_missing_credentials_without_secret_values(caplog):
    missing = FredConnector(api_key="", http_get=lambda url: "{}")
    report = missing.harvest_with_report("UNRATE")
    assert report["status"] == "blocked"
    assert report["diagnostics"] == [
        {"stage": "configuration", "code": "missing_credentials"}
    ]

    broken = FredConnector(
        api_key="do-not-print-this-key",
        http_get=lambda _url: (_ for _ in ()).throw(
            RuntimeError("do-not-print-this-key")
        ),
    )
    report = broken.harvest_with_report("UNRATE")
    assert report["status"] == "unavailable"
    assert report["diagnostics"][0]["stage"] == "fetch"
    assert "do-not-print-this-key" not in repr(report)
    list(broken.harvest("UNRATE"))
    assert "do-not-print-this-key" not in caplog.text

    invalid_query = broken.harvest_with_report({"unexpected": "field"})
    assert invalid_query["status"] == "unavailable"
    assert invalid_query["diagnostics"] == [
        {"stage": "discover", "code": "ValueError"}
    ]


def test_uses_env_key(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "env-key")
    conn = FredConnector(http_get=lambda url: json.dumps(HEADER) if "/series?" in url else json.dumps(OBS))
    assert conn.configured is True
    assert list(conn.harvest("UNRATE"))


def test_empty_header_yields_nothing():
    conn = _connector(header={"seriess": []})
    assert list(conn.harvest("UNRATE")) == []
