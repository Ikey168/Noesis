import duckdb
import pytest

from src.kb.quantitative import CALCULATE_SCOPE, WRITE_SCOPE, QuantitativeStore


def test_registered_pint_aliases_compounds_offsets_and_replay():
    store = QuantitativeStore(duckdb.connect())
    options = {"scopes": {CALCULATE_SCOPE, WRITE_SCOPE}, "principal_id": "test"}
    for symbol, factor in (("km/h", "0.2777777777777777777777777778"), ("m/s", "1")):
        store.register_unit(
            "berlin", symbol, {"length": 1, "time": -1}, factor=factor, **options
        )
    cases = [
        ("1.2", "kilometre", "m", "1200.000000"),
        ("0", "celsius", "K", "273.150000"),
        ("273.15", "K", "C", "0.000000"),
        ("25", "%", "fraction", "0.250000"),
        ("36", "km/h", "m/s", "10.000000"),
        ("1.2345665", "m", "m", "1.234566"),
    ]
    for value, source, target, expected in cases:
        native = store.convert("berlin", value, source, target, **options)
        candidate = store.convert(
            "berlin", value, source, target, backend="pint", **options
        )
        assert native["result"]["value"] == candidate["result"]["value"] == expected
        assert candidate["request"]["registry_hash"]
        assert candidate["input_ids"]
        assert store.convert(
            "berlin", value, source, target, backend="pint", **options
        )["idempotent"]
    # Pin old unit IDs: a later semantic version cannot alter replay.
    old = store.register_unit(
        "berlin",
        "local_length",
        {"length": 1},
        factor="2",
        aliases=["local"],
        **options,
    )
    original = store.convert("berlin", "3", "local", "m", backend="pint", **options)
    store.register_unit(
        "berlin",
        "local_length",
        {"length": 1},
        factor="3",
        aliases=["local"],
        semantic_version="2.0.0",
        **options,
    )
    replay = store.convert(
        "berlin", "3", old["unit_id"], "m", backend="pint", **options
    )
    assert original["calculation_id"] == replay["calculation_id"]
    assert (
        store.convert("berlin", "3", "local", "m", backend="pint", **options)["result"][
            "value"
        ]
        == "9.000000"
    )


@pytest.mark.parametrize(
    "source,target,code",
    [
        ("EUR", "USD", "economic_unit"),
        ("m", "s", "invalid_unit_conversion"),
        ("unknown", "m", "unknown_unit"),
    ],
)
def test_registered_units_fail_explicitly(source, target, code):
    store = QuantitativeStore(duckdb.connect())
    with pytest.raises(ValueError) as caught:
        store.convert(
            "berlin",
            "1",
            source,
            target,
            backend="pint",
            scopes={CALCULATE_SCOPE},
            principal_id="test",
        )
    assert caught.value.code == code
