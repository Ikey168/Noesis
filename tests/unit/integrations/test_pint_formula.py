import duckdb
import pytest

pytest.importorskip("pint")

from src.kb.quantitative import (
    CALCULATE_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    QuantitativeStore,
)

OPTIONS = {"scopes": {CALCULATE_SCOPE, WRITE_SCOPE}, "principal_id": "fixture"}


def metric(store, expression, unit, dimensions):
    return store.register_metric(
        "berlin",
        canonical_name="Authored formula " + expression,
        definition="Formula integration contract fixture",
        unit=unit,
        frequency="instant",
        population={"geography": "Berlin", "fixture": True},
        formula={"expression": expression, "input_dimensions": dimensions},
        **OPTIONS,
    )


def test_formula_carries_units_and_pins_receipts():
    store = QuantitativeStore(duckdb.connect())
    store.register_unit("berlin", "m/s", {"length": 1, "time": -1}, **OPTIONS)
    speed = metric(
        store,
        "distance / duration",
        "m/s",
        {"distance": {"length": 1}, "duration": {"time": 1}},
    )
    inputs = {
        "distance": {
            "value": "100",
            "unit_id": "m",
            "dimension": {"length": 1},
            "observation_id": "distance:1",
        },
        "duration": {
            "value": "10",
            "unit_id": "s",
            "dimension": {"time": 1},
            "observation_id": "duration:1",
        },
    }
    native = store.evaluate_formula("berlin", speed["metric_id"], inputs, **OPTIONS)
    candidate = store.evaluate_formula(
        "berlin", speed["metric_id"], inputs, backend="pint", **OPTIONS
    )
    assert native["result"]["value"] == candidate["result"]["value"] == "10.000000"
    assert candidate["request"]["registry_hash"]
    assert candidate["formula_revision_id"] == speed["revision_id"]
    assert {"distance:1", "duration:1"} <= set(candidate["input_ids"])
    assert store.replay_calculation(
        "berlin", candidate["calculation_id"], scopes={READ_SCOPE}
    )["deterministic"]
    assert store.evaluate_formula(
        "berlin", speed["metric_id"], inputs, backend="pint", **OPTIONS
    )["idempotent"]
    store.conn.close()


def test_scaled_units_percent_and_exact_decimal_literal():
    store = QuantitativeStore(duckdb.connect())
    total = metric(store, "a + b", "m", {"a": {"length": 1}, "b": {"length": 1}})
    inputs = {
        "a": {"value": "1", "unit_id": "km", "dimension": {"length": 1}},
        "b": {"value": "500", "unit_id": "m", "dimension": {"length": 1}},
    }
    assert (
        store.evaluate_formula(
            "berlin", total["metric_id"], inputs, backend="pint", **OPTIONS
        )["result"]["value"]
        == "1500.000000"
    )
    share = metric(
        store, "ratio * distance", "m", {"ratio": {}, "distance": {"length": 1}}
    )
    inputs = {
        "ratio": {"value": "25", "unit_id": "%", "dimension": {}},
        "distance": {"value": "200", "unit_id": "m", "dimension": {"length": 1}},
    }
    assert (
        store.evaluate_formula(
            "berlin", share["metric_id"], inputs, backend="pint", **OPTIONS
        )["result"]["value"]
        == "50.000000"
    )
    exact = metric(store, "a + 0.1234567890125000001", "fraction", {"a": {}})
    inputs = {"a": {"value": "0", "unit_id": "fraction", "dimension": {}}}
    assert (
        store.evaluate_formula(
            "berlin",
            exact["metric_id"],
            inputs,
            backend="pint",
            precision=12,
            **OPTIONS,
        )["result"]["value"]
        == "0.123456789013"
    )
    store.conn.close()


@pytest.mark.parametrize(
    "expression,unit,inputs,code",
    [
        (
            "a + b",
            "m",
            {"a": ("1", "m", {"length": 1}), "b": ("1", "s", {"time": 1})},
            "dimensional_error",
        ),
        (
            "a / b",
            "m",
            {"a": ("1", "m", {"length": 1}), "b": ("0", "fraction", {})},
            "arithmetic_error",
        ),
        ("a", "K", {"a": ("20", "C", {"temperature": 1})}, "offset_formula"),
        ("a", "EUR", {"a": ("1", "EUR", {"currency": 1})}, "economic_unit"),
        ("a", "m", {"a": ("1", "unknown", {"length": 1})}, "unknown_unit"),
    ],
)
def test_formula_errors_are_explicit(expression, unit, inputs, code):
    store = QuantitativeStore(duckdb.connect())
    model = metric(store, expression, unit, {k: v[2] for k, v in inputs.items()})
    values = {
        k: {"value": v[0], "unit_id": v[1], "dimension": v[2]}
        for k, v in inputs.items()
    }
    with pytest.raises(ValueError) as error:
        store.evaluate_formula(
            "berlin", model["metric_id"], values, backend="pint", **OPTIONS
        )
    assert error.value.code == code
    assert (
        store.conn.execute("SELECT count(*) FROM quantitative_calculations").fetchone()[
            0
        ]
        == 0
    )
    store.conn.close()


def test_formula_revision_unit_versions_and_unsafe_expression():
    from src.integrations.units import evaluate_registered_formula

    store = QuantitativeStore(duckdb.connect())
    old = store.register_unit(
        "berlin", "custom", {"length": 1}, factor="2", aliases=["local"], **OPTIONS
    )
    definition = metric(store, "a", "m", {"a": {"length": 1}})
    inputs = {
        "a": {"value": "3", "unit_id": old["unit_id"], "dimension": {"length": 1}}
    }
    original = store.evaluate_formula(
        "berlin", definition["metric_id"], inputs, backend="pint", **OPTIONS
    )
    store.register_unit(
        "berlin",
        "custom",
        {"length": 1},
        factor="3",
        semantic_version="2.0.0",
        aliases=["local"],
        **OPTIONS,
    )
    pinned = store.evaluate_formula(
        "berlin", definition["metric_id"], inputs, backend="pint", **OPTIONS
    )
    assert pinned["calculation_id"] == original["calculation_id"]
    inputs["a"]["unit_id"] = "local"
    latest = store.evaluate_formula(
        "berlin", definition["metric_id"], inputs, backend="pint", **OPTIONS
    )
    assert latest["result"]["value"] == "9.000000"
    assert original["result"]["value"] == "6.000000"
    target = store._unit("m", "berlin")
    for expression in ("__import__('os')", "a.real", "a ** 2"):
        with pytest.raises(ValueError) as error:
            evaluate_registered_formula(
                expression, {"a": {"value": "1", "unit_definition": target}}, target
            )
        assert error.value.code == "unsafe_formula"
    store.conn.close()
