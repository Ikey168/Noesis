import duckdb
import pytest

pytest.importorskip("pint")

from src.kb.pint_evaluation import PintUnitEvaluation
from src.kb.quantitative import QuantitativeError, QuantitativeStore

AUTH = {"principal_id": "scientist", "scopes": {"operator"}}


@pytest.mark.parametrize(
    "value,source,target,expected",
    [
        ("1.2345675", "km", "m", "1234.567500"),
        ("0", "C", "K", "273.150000"),
        ("273.15", "K", "C", "0.000000"),
        ("12.5", "percent", "ratio", "0.125000"),
        ("0.333333333333", "h", "s", "1200.000000"),
        ("1000", "g", "kg", "1.000000"),
    ],
)
def test_pint_matches_noesis_and_exact_reference(value, source, target, expected):
    conn = duckdb.connect()
    try:
        store = QuantitativeStore(conn)
        native = store.convert("scientific", value, source, target, **AUTH)
        candidate = PintUnitEvaluation(store).convert(
            "scientific", value, source, target, **AUTH
        )
        assert native["result"]["value"] == candidate["result"]["value"] == expected
        assert candidate["request"]["backend_version"] == "0.25.2"
        assert store.replay_calculation(
            "scientific", candidate["calculation_id"], scopes={"operator"}
        )
    finally:
        conn.close()


def test_compound_aliases_dimensions_and_isolated_unit_versions():
    conn = duckdb.connect()
    try:
        store = QuantitativeStore(conn)
        unit = store.register_unit(
            "scientific",
            "km/h",
            {"length": 1, "time": -1},
            factor="0.2777777777777777777777777778",
            aliases=["kph"],
            **AUTH,
        )
        store.register_unit(
            "scientific", "m/s", {"length": 1, "time": -1}, factor="1", **AUTH
        )
        adapter = PintUnitEvaluation(store)
        result = adapter.convert("scientific", "36", "kph", "m/s", **AUTH)
        assert result["result"]["value"] == "10.000000"
        assert unit["unit_id"] in result["input_ids"]
        assert adapter.product_dimensions(
            "scientific", {"kg": 1, "m": 2, "s": -2}, scopes={"operator"}
        ) == {"mass": 1, "length": 2, "time": -2}
        for source, target, code in [
            ("m", "s", "dimensional_error"),
            ("EUR", "USD", "unsupported_currency"),
            ("invented", "m", "unknown_unit"),
        ]:
            with pytest.raises(QuantitativeError) as failure:
                adapter.convert("scientific", "1", source, target, **AUTH)
            assert failure.value.code == code
        with pytest.raises(QuantitativeError, match="offset"):
            adapter.product_dimensions(
                "scientific", {"C": 1, "s": -1}, scopes={"operator"}
            )
    finally:
        conn.close()
