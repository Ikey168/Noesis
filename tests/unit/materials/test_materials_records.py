"""Material identity, structure, property value, condition set and method provenance records (MT02, #2080)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb import materials_records as mr
from tests.unit.materials import builders as b

ROOT = Path(__file__).resolve().parents[3]


def test_every_value_is_self_describing_and_validates_against_the_schema():
    record = b.entry(
        "materials-project",
        "mp-990001",
        [b.value("band_gap", "1.80", "eV", method=b.computed("GGA"))],
    )
    assert mr.validate(record) == record
    item = record["values"][0]
    assert {
        "property",
        "value",
        "unit",
        "conditions",
        "method",
        "status",
        "references",
    } <= set(item)
    assert item["conditions"]["temperature"] == {
        "status": "unstated"
    }  # never defaulted
    assert item["method"] == {
        "class": "computed",
        "method": "DFT (PAW)",
        "functional": "GGA",
        "code": "VASP",
        "code_version": "unstated",
    }
    assert record["release"] == {
        "label": "2099.1.0",
        "basis": "provider-stated",
        "released_on": "2099-01-15",
    }
    assert {"retrieved_at", "locator", "native_id", "provider"} <= set(record)
    schema = json.loads(
        (
            ROOT / "contracts/schemas/jsonschema/noesis-material-record-v1.json"
        ).read_text()
    )
    Draft7Validator.check_schema(schema)
    assert not list(Draft7Validator(schema).iter_errors(record))
    assert "None" not in json.dumps(record)


def test_formula_identity_and_conditions():
    assert (
        mr.material("- O2 Ti -")["reduced_formula"]
        == mr.material("TiO2")["reduced_formula"]
        == "O2Ti"
    )
    assert mr.material("Al4O6")["reduced_formula"] == "Al2O3"
    assert mr.atoms_per_formula_unit(mr.material("Al2O3")) == 5
    conditions = mr.condition_set(
        temperature={"value": "25", "unit": "°C"},
        pressure={"value": "1", "unit": "bar"},
        phase="solid",
    )
    assert conditions["temperature"]["normalized_value"] == "298.15"
    assert conditions["pressure"]["normalized_value"] == "100000"
    assert conditions["orientation"] == {"status": "unstated"}
    same = mr.condition_set(
        temperature={"value": "298.15", "unit": "K"},
        pressure={"value": "100", "unit": "kPa"},
        phase="Solid",
    )
    assert mr.condition_key(conditions) == mr.condition_key(
        same
    )  # source-independent key


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (
            lambda: mr.method_provenance("computed", method="DFT", functional="DFT"),
            "generic_method",
        ),
        (lambda: mr.method_provenance("predicted"), "invalid_method_class"),
        (lambda: b.entry("noesis", "x", []), "unknown_provider"),
        (
            lambda: b.entry(
                "materials-project",
                "mp-1",
                [
                    b.value(
                        "band_gap",
                        "1",
                        "eV",
                        method=mr.method_provenance(
                            "measured", technique="optical absorption"
                        ),
                    )
                ],
            ),
            "class_not_published",
        ),
        (
            lambda: b.entry("cod", "9990001", [b.value("band_gap", "1", "eV")]),
            "class_not_published",
        ),
        (
            lambda: mr.property_value(
                "made_up_property",
                "1",
                "eV",
                conditions=mr.condition_set(),
                method=b.computed("PBE"),
            ),
            "unknown_property",
        ),
    ],
)
def test_invalid_or_predicted_values_are_rejected(build, code):
    with pytest.raises(mr.MaterialRecordError) as error:
        build()
    assert error.value.code == code


def test_no_record_can_hold_a_noesis_prediction():
    record = b.entry(
        "oqmd", "990003", [b.value("band_gap", "1.9", "eV", method=b.computed("PBE"))]
    )
    record["values"][0]["predicted"] = "2.0"
    with pytest.raises(mr.MaterialRecordError) as error:
        mr.validate(record)
    assert error.value.code == "prediction_forbidden"
    with pytest.raises(mr.MaterialRecordError):
        mr.property_value(
            "band_gap",
            1.8,
            "eV",
            conditions=mr.condition_set(),
            method=b.computed("PBE"),
        )


def test_schemas_register_in_the_schema_registry():
    conn = duckdb.connect(":memory:")
    results = mr.register_schemas(
        conn, principal_id="operator", scopes={"knowledge:schema:register"}
    )
    assert len(results) == 2
    again = mr.register_schemas(
        conn, principal_id="operator", scopes={"knowledge:schema:register"}
    )
    assert [r.get("module_id") or r.get("id") for r in again] == [
        r.get("module_id") or r.get("id") for r in results
    ]
