"""Unit tests for the World Bank dataset connector (offline, injected HTTP)."""

from __future__ import annotations

import json

from services.ingest.common.series_model import SeriesRecord
from src.ingestion.connectors.dataset.worldbank import WorldBankConnector

# A trimmed but shape-accurate World Bank v2 response for
# GET /v2/country/DE/indicator/SL.UEM.TOTL.ZS?format=json — pagination page
# first, then the observation rows (returned most-recent-first by the API).
WB_PAYLOAD = json.dumps(
    [
        {"page": 1, "pages": 1, "per_page": 20000, "total": 3, "lastupdated": "2025-01-01"},
        [
            {
                "indicator": {"id": "SL.UEM.TOTL.ZS", "value": "Unemployment, total (% of total labor force)"},
                "country": {"id": "DE", "value": "Germany"},
                "countryiso3code": "DEU",
                "date": "2024",
                "value": 3.4,
            },
            {
                "indicator": {"id": "SL.UEM.TOTL.ZS", "value": "Unemployment, total (% of total labor force)"},
                "country": {"id": "DE", "value": "Germany"},
                "countryiso3code": "DEU",
                "date": "2023",
                "value": 3.02,
            },
            {
                "indicator": {"id": "SL.UEM.TOTL.ZS", "value": "Unemployment, total (% of total labor force)"},
                "country": {"id": "DE", "value": "Germany"},
                "countryiso3code": "DEU",
                "date": "2022",
                "value": None,
            },
        ],
    ]
)


def _connector(payload: str = WB_PAYLOAD) -> WorldBankConnector:
    calls = []

    def fake_get(url: str) -> str:
        calls.append(url)
        return payload

    conn = WorldBankConnector(http_get=fake_get)
    conn._calls = calls  # type: ignore[attr-defined]
    return conn


def test_harvest_builds_contract_series():
    conn = _connector()
    records = list(conn.harvest(("SL.UEM.TOTL.ZS", "DE")))
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, SeriesRecord)
    assert rec.series_id == "wb:SL.UEM.TOTL.ZS:DE"
    assert rec.provider == "worldbank"
    assert rec.geography == "DE"
    assert rec.frequency == "annual"
    assert rec.unit == "percent"  # inferred from the "%" in the indicator name
    assert rec.license == "CC-BY-4.0"
    # Observations sorted ascending; the reported gap is preserved as None.
    assert [(o.period, o.value) for o in rec.observations] == [
        ("2022", None),
        ("2023", 3.02),
        ("2024", 3.4),
    ]
    # lastupdated 2025-01-01 -> ms epoch
    assert rec.as_of == 1735689600000


def test_url_targets_country_indicator_endpoint():
    conn = _connector()
    list(conn.harvest({"indicator": "SL.UEM.TOTL.ZS", "geography": "DE"}))
    assert conn._calls  # type: ignore[attr-defined]
    url = conn._calls[0]  # type: ignore[attr-defined]
    assert "/country/DE/indicator/SL.UEM.TOTL.ZS" in url
    assert "format=json" in url


def test_multiple_specs_yield_multiple_series():
    conn = _connector()
    records = list(conn.harvest([("SL.UEM.TOTL.ZS", "DE"), ("SL.UEM.TOTL.ZS", "FR")]))
    assert len(records) == 2


def test_no_data_payload_yields_nothing():
    # World Bank signals "no data" with a message page and a null body.
    payload = json.dumps([{"message": [{"id": "120", "value": "no data"}]}, None])
    conn = _connector(payload)
    assert list(conn.harvest(("BAD.INDICATOR", "DE"))) == []


def test_fetch_failure_is_skipped_not_raised():
    def boom(url: str) -> str:
        raise ConnectionError("network down")

    conn = WorldBankConnector(http_get=boom)
    # harvest() must swallow the fetch error and yield nothing rather than raise.
    assert list(conn.harvest(("SL.UEM.TOTL.ZS", "DE"))) == []
