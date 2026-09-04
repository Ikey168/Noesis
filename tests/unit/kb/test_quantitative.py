from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.quantitative import QuantitativeError, QuantitativeStore

READ = {"knowledge:quantitative:read"}
WRITE = {"knowledge:quantitative:write"}
CALCULATE = {"knowledge:quantitative:calculate"}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _metric(store, namespace="economic", **updates):
    values = {
        "canonical_name": "Gross domestic product",
        "definition": "Market value of final goods and services.",
        "unit": "EUR",
        "frequency": "quarterly",
        "population": {"geography": "DE"},
        "synonyms": ["GDP", "output"],
        "mappings": {"fred": "DEUGDPNQDSMEI"},
    }
    values.update(updates)
    return store.register_metric(
        namespace, principal_id="analyst", scopes=WRITE, **values
    )


def _observe(store, metric_id, *, vintage="v1", provider="destatis", **updates):
    values = {
        "period": "2026-Q1",
        "value": "100.25",
        "provider_series_id": "gdp-q",
        "release_at_ms": 100,
        "retrieved_at_ms": 110,
        "valid_from_ms": 100,
        "adjustment": "seasonally-adjusted",
    }
    values.update(updates)
    return store.observe(
        "economic",
        metric_id,
        provider=provider,
        vintage_id=vintage,
        principal_id="analyst",
        scopes=WRITE,
        **values,
    )


def test_versioned_unit_metric_registries_synonyms_compounds_and_mappings():
    conn = duckdb.connect(":memory:")
    store = QuantitativeStore(conn, now=lambda: 1000)
    speed = store.register_unit(
        "scientific",
        "km/h",
        {"length": 1, "time": -1},
        factor="0.2777777777777777777777777778",
        aliases=["kilometres per hour", "kph"],
        principal_id="scientist",
        scopes=WRITE,
    )
    assert speed["dimension"] == {"length": 1, "time": -1}
    store.register_unit(
        "scientific",
        "m/s",
        {"length": 1, "time": -1},
        factor="1",
        aliases=["metres per second"],
        principal_id="scientist",
        scopes=WRITE,
    )
    converted = store.convert(
        "scientific",
        "36",
        "km/h",
        "m/s",
        precision=6,
        principal_id="scientist",
        scopes=CALCULATE,
    )
    assert converted["result"]["value"] == "10.000000"
    assert store.register_unit(
        "scientific",
        "km/h",
        {"length": 1, "time": -1},
        factor="0.2777777777777777777777777778",
        aliases=["kph", "kilometres per hour"],
        principal_id="scientist",
        scopes=WRITE,
    )["idempotent"]
    with pytest.raises(QuantitativeError, match="different content"):
        store.register_unit(
            "scientific",
            "km/h",
            {"length": 1, "time": -1},
            factor="2",
            principal_id="scientist",
            scopes=WRITE,
        )
    metric = _metric(store)
    assert (
        store.discover("economic", scopes=READ, query="GDP")[0]["metric_id"]
        == metric["metric_id"]
    )
    assert (
        store.discover("economic", scopes=READ, query="DEUGDP")[0]["mappings"]["fred"]
        == "DEUGDPNQDSMEI"
    )
    revised = store.revise_metric(
        "economic",
        metric["metric_id"],
        1,
        {"population": {}, "mappings": {}, "synonyms": ["GDP revised"]},
        principal_id="curator",
        scopes=WRITE,
    )
    history = store.metric(
        "economic", metric["metric_id"], scopes=READ, include_history=True
    )
    assert revised["revision"] == 2 and revised["population"] == {}
    assert [item["revision"] for item in history["history"]] == [2, 1]
    _validate("noesis-quantitative-metric-v1.json", metric)
    conn.close()


def test_observation_vintages_preliminary_missing_provider_conflicts_and_projection():
    conn = duckdb.connect(":memory:")
    store = QuantitativeStore(conn, now=lambda: 1000)
    metric = _metric(store)
    preliminary = _observe(store, metric["metric_id"], preliminary=True)
    assert _observe(store, metric["metric_id"], preliminary=True)["idempotent"]
    with pytest.raises(QuantitativeError, match="different content"):
        _observe(store, metric["metric_id"], value="999")
    with pytest.raises(QuantitativeError, match="same metric"):
        _observe(
            store,
            metric["metric_id"],
            vintage="bad",
            revision_of="quantitative-observation:missing",
        )
    final = _observe(
        store,
        metric["metric_id"],
        vintage="v2",
        value="101.5",
        preliminary=False,
        revision_of=preliminary["observation_id"],
        release_at_ms=120,
        retrieved_at_ms=130,
    )
    missing = _observe(
        store,
        metric["metric_id"],
        vintage="v3",
        period="2026-Q2",
        value=None,
        release_at_ms=140,
        retrieved_at_ms=150,
    )
    other = _observe(
        store,
        metric["metric_id"],
        provider="oecd",
        vintage="o1",
        value="98",
    )
    assert missing["missing"] and missing["value"] is None
    assert other["observation_id"] != preliminary["observation_id"]
    assert [
        item["vintage_id"]
        for item in store.series(
            "economic", metric["metric_id"], scopes=READ, provider="destatis"
        )
    ] == ["v2", "v3"]
    assert [
        item["vintage_id"]
        for item in store.series(
            "economic",
            metric["metric_id"],
            scopes=READ,
            provider="destatis",
            as_of_ms=115,
        )
    ] == ["v1"]
    assert (
        len(
            store.series(
                "economic", metric["metric_id"], scopes=READ, include_vintages=True
            )
        )
        == 4
    )
    assert conn.execute("SELECT count(*) FROM dataset_observations").fetchone()[0] == 4
    assert final["currency_code"] == "EUR"
    _validate("noesis-quantitative-observation-v1.json", final)
    conn.close()


def test_exact_conversions_currency_redenomination_inflation_frequency_and_replay():
    conn = duckdb.connect(":memory:")
    ticks = iter(range(1000, 1100))
    store = QuantitativeStore(conn, now=lambda: next(ticks))
    metres = store.convert(
        "scientific",
        "1.2345675",
        "km",
        "m",
        precision=3,
        principal_id="scientist",
        scopes=CALCULATE,
    )
    assert metres["result"]["value"] == "1234.568"
    repeat = store.convert(
        "scientific",
        "1.2345675",
        "km",
        "m",
        precision=3,
        principal_id="other",
        scopes=CALCULATE,
    )
    assert repeat["idempotent"] and repeat["created_at_ms"] == metres["created_at_ms"]
    with pytest.raises(QuantitativeError, match="exact matching currency rate"):
        store.convert("economic", 10, "USD", "EUR", principal_id="a", scopes=CALCULATE)
    fx = store.convert(
        "economic",
        10,
        "USD",
        "EUR",
        precision=2,
        rate={"rate_id": "ecb:2026-01-01", "from": "USD", "to": "EUR", "rate": "0.91"},
        principal_id="a",
        scopes=CALCULATE,
    )
    assert fx["result"]["value"] == "9.10"
    euro = store._unit("EUR", "economic")
    store.register_unit(
        "economic",
        "DEM",
        {"currency": 1},
        currency_code="DEM",
        successor_unit_id=euro["unit_id"],
        redenomination_factor="0.5112918811962185",
        principal_id="a",
        scopes=WRITE,
    )
    redenominated = store.convert(
        "economic",
        "1.95583",
        "DEM",
        "EUR",
        precision=5,
        principal_id="a",
        scopes=CALCULATE,
    )
    assert redenominated["result"]["value"] == "1.00000"
    frequency = store.transform_frequency(
        "economic",
        [
            {"value": "1", "observation_id": "o1"},
            {"value": "2", "observation_id": "o2"},
        ],
        from_frequency="monthly",
        to_frequency="quarterly",
        aggregation="average",
        principal_id="a",
        scopes=CALCULATE,
    )
    assert frequency["result"]["value"] == "1.500000"
    adjusted = store.adjust_inflation(
        "economic",
        "100",
        {"value": "100", "period": "2020", "observation_id": "cpi-old"},
        {"value": "125", "period": "2026", "observation_id": "cpi-new"},
        principal_id="a",
        scopes=CALCULATE,
    )
    assert adjusted["result"] == {"value": "125.000000", "price_basis": "2026"}
    assert store.replay_calculation(
        "economic", adjusted["calculation_id"], scopes=READ
    )["deterministic"]
    _validate("noesis-quantitative-calculation-v1.json", adjusted)
    with pytest.raises(QuantitativeError, match="incompatible dimensions"):
        store.convert("scientific", 1, "kg", "m", principal_id="a", scopes=CALCULATE)
    conn.close()


def test_derived_metric_formula_is_safe_dimension_checked_and_revision_pinned():
    conn = duckdb.connect(":memory:")
    store = QuantitativeStore(conn, now=lambda: 1000)
    metric = _metric(
        store,
        canonical_name="Mean sample temperature",
        definition="Arithmetic mean of two temperature measurements.",
        unit="K",
        frequency="instant",
        population={"sample": "reactor-a"},
        synonyms=[],
        mappings={"lab": "TEMP_MEAN"},
        formula={
            "expression": "(a + b) / 2",
            "input_dimensions": {"a": {"temperature": 1}, "b": {"temperature": 1}},
        },
        namespace="scientific",
    )
    result = store.evaluate_formula(
        "scientific",
        metric["metric_id"],
        {
            "a": {
                "value": "300",
                "dimension": {"temperature": 1},
                "observation_id": "sensor:a",
            },
            "b": {
                "value": "302",
                "dimension": {"temperature": 1},
                "observation_id": "sensor:b",
            },
        },
        principal_id="scientist",
        scopes=CALCULATE,
    )
    assert result["result"]["value"] == "301.000000"
    assert result["formula_revision_id"] == metric["revision_id"]
    with pytest.raises(QuantitativeError, match="incompatible dimension"):
        store.evaluate_formula(
            "scientific",
            metric["metric_id"],
            {
                "a": {"value": 1, "dimension": {"mass": 1}},
                "b": {"value": 2, "dimension": {"temperature": 1}},
            },
            principal_id="scientist",
            scopes=CALCULATE,
        )
    dangerous = store.revise_metric(
        "scientific",
        metric["metric_id"],
        1,
        {"formula": {"expression": "__import__('os')", "input_dimensions": {}}},
        principal_id="scientist",
        scopes=WRITE,
    )
    with pytest.raises(QuantitativeError, match="unsupported expression"):
        store.evaluate_formula(
            "scientific",
            dangerous["metric_id"],
            {},
            principal_id="scientist",
            scopes=CALCULATE,
        )
    conn.close()


@pytest.mark.parametrize(
    "break_type",
    ["definition", "methodology", "geography", "rebase", "basket", "provider-switch"],
)
def test_comparability_flags_every_supported_break(break_type):
    conn = duckdb.connect(":memory:")
    store = QuantitativeStore(conn, now=lambda: 1000)
    metric = _metric(store)
    left = _observe(store, metric["metric_id"], valid_from_ms=100)
    right = _observe(
        store,
        metric["metric_id"],
        vintage="v2",
        value="110",
        valid_from_ms=300,
        release_at_ms=300,
        retrieved_at_ms=310,
    )
    created = store.add_break(
        "economic",
        metric["metric_id"],
        break_type,
        200,
        before={"definition": "old"},
        after={"definition": "new"},
        evidence=[{"citation": "methodology:2026"}],
        confidence=0.9,
        principal_id="curator",
        scopes=WRITE,
    )
    assert store.add_break(
        "economic",
        metric["metric_id"],
        break_type,
        200,
        before={"definition": "old"},
        after={"definition": "new"},
        evidence=[{"citation": "methodology:2026"}],
        confidence=0.9,
        principal_id="curator",
        scopes=WRITE,
    )["idempotent"]
    assessment = store.comparability(
        "economic", left["observation_id"], right["observation_id"], scopes=READ
    )
    assert created["break_id"] == assessment["breaks"][0]["break_id"]
    assert (
        not assessment["comparable"]
        and f"series-break:{break_type}" in assessment["reasons"]
    )
    _validate("noesis-quantitative-comparability-v1.json", assessment)
    conn.close()


def test_adjustment_provider_switch_and_auth_boundaries():
    conn = duckdb.connect(":memory:")
    store = QuantitativeStore(conn, now=lambda: 1000)
    metric = _metric(store)
    left = _observe(store, metric["metric_id"], valid_from_ms=100)
    right = _observe(
        store,
        metric["metric_id"],
        provider="oecd",
        vintage="v2",
        adjustment="not-adjusted",
        valid_from_ms=300,
    )
    result = store.comparability(
        "economic", left["observation_id"], right["observation_id"], scopes=READ
    )
    assert result["reasons"] == [
        "seasonal-adjustment-mismatch",
        "unreviewed-provider-switch",
    ]
    with pytest.raises(QuantitativeError, match="required scope"):
        store.discover("economic", scopes=set())
    with pytest.raises(QuantitativeError, match="required scope"):
        store.register_metric(
            "secret",
            "Classified metric",
            "Should not be created without authorization.",
            "count",
            principal_id="intruder",
            scopes=set(),
        )
    conn.close()
