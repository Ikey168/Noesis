"""Unit tests for the DuckDB observation store, including vintage idempotency."""

from __future__ import annotations

import pytest

from services.ingest.common.series_model import Observation, SeriesRecord
from src.ingestion.connectors.dataset.store import ObservationStore


def _record(as_of: int, obs, title="Unemployment - Germany") -> SeriesRecord:
    return SeriesRecord(
        series_id="wb:SL.UEM.TOTL.ZS:DE",
        provider="worldbank",
        title=title,
        frequency="annual",
        as_of=as_of,
        observations=[Observation(p, v) for p, v in obs],
        unit="percent",
        geography="DE",
        license="CC-BY-4.0",
    )


@pytest.fixture()
def store():
    duckdb = pytest.importorskip("duckdb")
    return ObservationStore(duckdb.connect(":memory:"))


def test_upsert_and_read_back(store):
    written = store.upsert(_record(100, [("2023", 3.02), ("2024", 3.4)]))
    assert written == 2
    header = store.get_series("wb:SL.UEM.TOTL.ZS:DE")
    assert header["provider"] == "worldbank"
    assert header["geography"] == "DE"
    assert header["metadata"] == {}
    obs = store.get_observations("wb:SL.UEM.TOTL.ZS:DE")
    assert [(o.period, o.value) for o in obs] == [("2023", 3.02), ("2024", 3.4)]


def test_reharvest_same_vintage_is_idempotent(store):
    rec = _record(100, [("2023", 3.02), ("2024", 3.4)])
    store.upsert(rec)
    store.upsert(rec)  # same vintage again
    assert store.vintages("wb:SL.UEM.TOTL.ZS:DE") == [100]
    obs = store.get_observations("wb:SL.UEM.TOTL.ZS:DE")
    assert len(obs) == 2  # not duplicated


def test_new_vintage_retained_alongside_prior(store):
    store.upsert(_record(100, [("2024", 3.4)]))
    # A later harvest revises 2024 and adds 2025.
    store.upsert(_record(200, [("2024", 3.5), ("2025", 3.6)], title="Unemployment - Germany (rev)"))
    assert store.vintages("wb:SL.UEM.TOTL.ZS:DE") == [100, 200]
    # Default read returns the latest vintage as a coherent series.
    latest = store.get_observations("wb:SL.UEM.TOTL.ZS:DE")
    assert [(o.period, o.value) for o in latest] == [("2024", 3.5), ("2025", 3.6)]
    # The prior vintage is still queryable for replay.
    prior = store.get_observations("wb:SL.UEM.TOTL.ZS:DE", as_of=100)
    assert [(o.period, o.value) for o in prior] == [("2024", 3.4)]
    # Header reflects the newest harvest.
    assert store.get_series("wb:SL.UEM.TOTL.ZS:DE")["as_of"] == 200
    assert "rev" in store.get_series("wb:SL.UEM.TOTL.ZS:DE")["title"]


def test_list_series_filters(store):
    store.upsert(_record(100, [("2024", 3.4)]))
    assert len(store.list_series()) == 1
    assert len(store.list_series(provider="worldbank")) == 1
    assert len(store.list_series(geography="FR")) == 0


def test_persists_to_file(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    path = str(tmp_path / "series.duckdb")
    store = ObservationStore(duckdb.connect(path))
    store.upsert(_record(100, [("2024", 3.4)]))
    store._conn.close()
    # Reopen the same file and confirm the data survived.
    reopened = ObservationStore(duckdb.connect(path))
    assert reopened.get_series("wb:SL.UEM.TOTL.ZS:DE") is not None
    assert [(o.period, o.value) for o in reopened.get_observations("wb:SL.UEM.TOTL.ZS:DE")] == [("2024", 3.4)]
