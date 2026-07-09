"""Unit tests for the Eurostat JSON-stat 2.0 connector (offline)."""

from __future__ import annotations

import json

from services.ingest.common.series_model import SeriesRecord
from src.ingestion.connectors.dataset.eurostat import EurostatConnector, _strides

# A single-geography annual cube: dims freq(1) x unit(1) x geo(1) x time(3).
# value is flattened row-major, so with all non-time dims singletons the flat
# index equals the time index.
SIMPLE_CUBE = {
    "version": "2.0",
    "class": "dataset",
    "label": "Unemployment rate",
    "updated": "2025-01-15",
    "id": ["freq", "unit", "geo", "time"],
    "size": [1, 1, 1, 3],
    "dimension": {
        "freq": {"category": {"index": {"A": 0}, "label": {"A": "Annual"}}},
        "unit": {"category": {"index": {"PC": 0}, "label": {"PC": "Percentage"}}},
        "geo": {"category": {"index": {"DE": 0}, "label": {"DE": "Germany"}}},
        "time": {"category": {"index": {"2022": 0, "2023": 1, "2024": 2}, "label": {}}},
    },
    "value": {"0": 3.1, "1": 3.0, "2": 3.4},
}


def _connector(cube=SIMPLE_CUBE):
    return EurostatConnector(http_get=lambda url: json.dumps(cube))


def test_strides_row_major():
    assert _strides([1, 1, 1, 3]) == [3, 3, 3, 1]
    assert _strides([2, 3, 4]) == [12, 4, 1]


def test_harvest_simple_annual_series():
    conn = _connector()
    records = list(conn.harvest({"dataset": "une_rt_a", "geography": "DE"}))
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, SeriesRecord)
    assert rec.series_id == "estat:une_rt_a:DE"
    assert rec.provider == "eurostat"
    assert rec.geography == "DE"
    assert rec.frequency == "annual"
    assert rec.unit == "percent"
    assert [(o.period, o.value) for o in rec.observations] == [
        ("2022", 3.1),
        ("2023", 3.0),
        ("2024", 3.4),
    ]


def test_flat_index_with_multiple_dimensions():
    # geo has one code but 'indic' dim has two categories; the connector picks
    # the first (index 0) and must compute the flat index correctly so the
    # right time-slice values are read.
    cube = {
        "id": ["indic", "geo", "time"],
        "size": [2, 1, 3],
        "label": "Two-indicator cube",
        "dimension": {
            "indic": {"category": {"index": {"A": 0, "B": 1}, "label": {"A": "Indic A", "B": "Indic B"}}},
            "geo": {"category": {"index": {"FR": 0}, "label": {"FR": "France"}}},
            "time": {"category": {"index": {"2022": 0, "2023": 1, "2024": 2}, "label": {}}},
        },
        # strides = [3,3,1]. indic=0 slice -> flat 0,1,2 ; indic=1 slice -> 3,4,5
        "value": {"0": 10.0, "1": 11.0, "2": 12.0, "3": 90.0, "4": 91.0, "5": 92.0},
    }
    rec = list(_connector(cube).harvest({"dataset": "x", "geography": "FR"}))[0]
    # Picks indic index 0 -> the 10/11/12 slice, not the B slice.
    assert [o.value for o in rec.observations] == [10.0, 11.0, 12.0]
    assert rec.metadata.get("collapsed_dimensions") == {"indic": "Indic A"}


def test_quarterly_period_normalization():
    cube = json.loads(json.dumps(SIMPLE_CUBE))
    cube["dimension"]["freq"]["category"] = {"index": {"Q": 0}, "label": {"Q": "Quarterly"}}
    cube["dimension"]["time"]["category"]["index"] = {"2023Q1": 0, "2023Q2": 1, "2023Q3": 2}
    rec = list(_connector(cube).harvest({"dataset": "x", "geography": "DE"}))[0]
    assert rec.frequency == "quarterly"
    assert [o.period for o in rec.observations] == ["2023-Q1", "2023-Q2", "2023-Q3"]


def test_empty_cube_yields_nothing():
    assert list(_connector({"id": [], "size": []}).harvest({"dataset": "x", "geography": "DE"})) == []


def test_url_includes_geo_and_format():
    calls = []
    conn = EurostatConnector(http_get=lambda url: calls.append(url) or json.dumps(SIMPLE_CUBE))
    list(conn.harvest({"dataset": "une_rt_a", "geography": "DE", "sex": "T"}))
    assert calls and "une_rt_a" in calls[0]
    assert "geo=DE" in calls[0] and "format=JSON" in calls[0] and "sex=T" in calls[0]
