"""Unit tests for the panel-facing dataset queries (A2)."""

from __future__ import annotations

import pytest

from services.ingest.common.series_model import Observation, SeriesRecord
from src.ingestion.connectors.dataset import queries
from src.ingestion.connectors.dataset.store import ObservationStore


@pytest.fixture()
def conn():
    duckdb = pytest.importorskip("duckdb")
    return duckdb.connect(":memory:")


def _seed(conn):
    store = ObservationStore(conn)
    store.upsert(
        SeriesRecord(
            series_id="wb:SL.UEM.TOTL.ZS:DE",
            provider="worldbank",
            title="Unemployment, total (% of labor force) - Germany",
            frequency="annual",
            as_of=100,
            observations=[Observation("2022", 3.1), Observation("2023", 3.02), Observation("2024", 3.4)],
            unit="percent",
            geography="DE",
        )
    )
    store.upsert(
        SeriesRecord(
            series_id="wb:NY.GDP.MKTP.CD:FR",
            provider="worldbank",
            title="GDP (current US$) - France",
            frequency="annual",
            as_of=100,
            observations=[Observation("2023", 3.0e12)],
            unit="usd",
            geography="FR",
        )
    )
    return store


def test_available_false_on_empty_warehouse(conn):
    assert queries.available(conn) is False
    # Panel/read functions degrade to empty, not error.
    assert queries.list_series(conn)["series"] == []
    assert queries.series_explorer(conn)["series"] == []
    assert "note" in queries.get_observations(conn, "wb:X:DE")


def test_list_series_and_filters(conn):
    _seed(conn)
    assert queries.available(conn) is True
    assert queries.list_series(conn)["count"] == 2
    assert queries.list_series(conn, geography="DE")["count"] == 1
    assert queries.list_series(conn, provider="worldbank")["count"] == 2
    # substring on title/id, case-insensitive
    de = queries.list_series(conn, query="unemployment")
    assert de["count"] == 1
    assert de["series"][0]["series_id"] == "wb:SL.UEM.TOTL.ZS:DE"


def test_get_series_summary(conn):
    _seed(conn)
    header = queries.get_series(conn, "wb:SL.UEM.TOTL.ZS:DE")
    assert header["provider"] == "worldbank"
    assert header["observation_count"] == 3
    assert header["first_period"] == "2022"
    assert header["last_period"] == "2024"
    assert header["vintages"] == 1
    assert queries.get_series(conn, "wb:missing")["error"]


def test_get_observations_latest_and_vintage(conn):
    store = _seed(conn)
    # Add a newer vintage revising 2024 and adding 2025.
    store.upsert(
        SeriesRecord(
            series_id="wb:SL.UEM.TOTL.ZS:DE",
            provider="worldbank",
            title="Unemployment - Germany",
            frequency="annual",
            as_of=200,
            observations=[Observation("2024", 3.5), Observation("2025", 3.6)],
            unit="percent",
            geography="DE",
        )
    )
    latest = queries.get_observations(conn, "wb:SL.UEM.TOTL.ZS:DE")
    assert latest["as_of"] == 200
    assert [(o["period"], o["value"]) for o in latest["observations"]] == [("2024", 3.5), ("2025", 3.6)]
    prior = queries.get_observations(conn, "wb:SL.UEM.TOTL.ZS:DE", as_of=100)
    assert len(prior["observations"]) == 3


def test_get_observations_caps(conn, monkeypatch):
    store = ObservationStore(conn)
    obs = [Observation(str(2000 + i), float(i)) for i in range(5)]
    store.upsert(
        SeriesRecord(
            series_id="wb:X:DE", provider="worldbank", title="X",
            frequency="annual", as_of=1, observations=obs, geography="DE",
        )
    )
    monkeypatch.setattr(queries, "MAX_OBS", 3)
    out = queries.get_observations(conn, "wb:X:DE", limit=3)
    assert len(out["observations"]) == 3
    assert out["truncated"] is True


def test_series_explorer_summaries(conn):
    _seed(conn)
    payload = queries.series_explorer(conn)
    assert payload["count"] == 2
    de = next(s for s in payload["series"] if s["series_id"] == "wb:SL.UEM.TOTL.ZS:DE")
    assert de["observation_count"] == 3
    assert de["latest_value"] == 3.4
    assert de["last_period"] == "2024"
    # topic filter narrows
    assert queries.series_explorer(conn, topic="gdp")["count"] == 1
