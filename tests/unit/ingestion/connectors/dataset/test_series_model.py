"""Unit tests for the dataset-series-v1 model and its contract alignment."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.ingest.common.series_model import Observation, SeriesRecord

REPO_ROOT = Path(__file__).resolve().parents[5]
SCHEMA_PATH = REPO_ROOT / "contracts" / "schemas" / "jsonschema" / "dataset-series-v1.json"


def _valid_record() -> SeriesRecord:
    return SeriesRecord(
        series_id="wb:SL.UEM.TOTL.ZS:DE",
        provider="worldbank",
        title="Unemployment, total (% of labor force) - Germany",
        frequency="annual",
        as_of=1767225600000,
        observations=[Observation("2023", 3.02), Observation("2024", 3.4)],
        unit="percent",
        geography="DE",
        license="CC-BY-4.0",
    )


def test_roundtrip_to_dict_from_dict():
    record = _valid_record()
    payload = record.to_dict()
    assert payload["series_id"] == "wb:SL.UEM.TOTL.ZS:DE"
    assert payload["observations"] == [
        {"period": "2023", "value": 3.02},
        {"period": "2024", "value": 3.4},
    ]
    rebuilt = SeriesRecord.from_dict(payload)
    assert rebuilt.to_dict() == payload


def test_from_dict_ignores_unknown_keys():
    payload = _valid_record().to_dict()
    payload["_extra"] = "ignored"
    rebuilt = SeriesRecord.from_dict(payload)
    assert rebuilt.series_id == "wb:SL.UEM.TOTL.ZS:DE"


def test_observations_accept_dicts():
    record = SeriesRecord(
        series_id="wb:X:DE",
        provider="worldbank",
        title="X",
        frequency="annual",
        as_of=1,
        observations=[{"period": "2024", "value": None}],
    )
    assert record.observations[0].value is None
    assert record.observations[0].period == "2024"


@pytest.mark.parametrize(
    "field,value",
    [
        ("series_id", ""),
        ("provider", ""),
        ("title", ""),
        ("frequency", "fortnightly"),
        ("as_of", -1),
    ],
)
def test_invalid_fields_rejected(field, value):
    kwargs = dict(
        series_id="wb:X:DE",
        provider="worldbank",
        title="X",
        frequency="annual",
        as_of=1,
    )
    kwargs[field] = value
    with pytest.raises(ValueError):
        SeriesRecord(**kwargs)


def test_record_validates_against_jsonschema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft7Validator(schema).validate(_valid_record().to_dict())


def test_schema_example_validates_and_roundtrips():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text())
    for example in schema.get("examples", []):
        jsonschema.Draft7Validator(schema).validate(example)
        # Every schema example must be constructible by the model.
        SeriesRecord.from_dict(example)
