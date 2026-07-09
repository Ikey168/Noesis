"""Unit tests for the FRED dataset connector (offline, injected HTTP)."""

from __future__ import annotations

import json

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


def test_geography_spec_included_in_id():
    conn = _connector()
    records = list(conn.harvest({"series": "UNRATE", "geography": "US"}))
    assert records[0].series_id == "fred:UNRATE:US"
    assert records[0].geography == "US"


def test_not_configured_yields_nothing():
    # No key -> skip-with-warning, harvest returns nothing (does not raise).
    conn = FredConnector(api_key="", http_get=lambda url: "{}")
    assert conn.configured is False
    assert list(conn.harvest("UNRATE")) == []


def test_uses_env_key(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "env-key")
    conn = FredConnector(http_get=lambda url: json.dumps(HEADER) if "/series?" in url else json.dumps(OBS))
    assert conn.configured is True
    assert list(conn.harvest("UNRATE"))


def test_empty_header_yields_nothing():
    conn = _connector(header={"seriess": []})
    assert list(conn.harvest("UNRATE")) == []
