"""FC02 (#2251): food composition records, immutable label revisions, units and round trips."""

from __future__ import annotations

import copy
import json

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.food_composition import (
    CONTRACT,
    RECORD_TYPES,
    FoodCompositionError,
    FoodCompositionStore,
    gtin_key,
    normalize_unit,
    validate_statement,
)
from tests.unit import food_composition_harness as h

NS = h.NS


def statement(**changes):
    value = {
        "contract": CONTRACT, "provider": "open-food-facts", "provider_key": "off:4000000000150",
        "food_kind": "food-product", "provenance_class": "crowd-sourced",
        "revision": {"value": "3", "basis": "off-revision", "date": "2026-01-10", "declared": "1768003200"},
        "identifiers": {"gtin": {"value": "4000000000150", "state": "valid", "key": "04000000000150"}},
        "names": {"product_name": "Greek style yoghurt", "brands": ["Dairyvale"]},
        "ingredient_statements": [{"text": "Milk, cream", "language": "en", "locator": {"json_pointer": "/p/i"}}],
        "allergen_declarations": [{"relation": "contains", "value": "en:milk", "declared_as": "tag"}],
        "nutrient_values": [{
            "nutrient": {"scheme": "off-nutriment", "id": "proteins", "name": "proteins", "tagname": None},
            "amount": "5.7", "amount_decimal": "5.7", "unit": normalize_unit("g"), "basis": "per 100 g",
            "derivation": None, "value_kind": "off-nutriment", "value_flags": {}}],
        "labelling_claims": [{"text": "en:vegetarian", "tag": "en:vegetarian"}],
        "source_fields": {"completeness": 0.9},
        "attribution": {"licence": "ODbL-1.0", "text": "Open Food Facts", "share_alike": True},
        "url": "https://world.openfoodfacts.org/product/4000000000150",
    }
    value.update(changes)
    return value


def test_schema_is_draft7_with_the_seven_record_types():
    schema = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-food-composition-record-v1.json").read_text())
    Draft7Validator.check_schema(schema)
    assert tuple(schema["properties"]["record_type"]["enum"]) == RECORD_TYPES
    assert set(schema["required"]) >= {"provider", "provider_key", "revision", "retrieved_at", "as_of",
                                       "provenance_class"}
    from src.kb.schema_registry import _builtin_definitions

    assert any(m["name"] == "food-composition-record" and m["semantic_version"] == "1.0.0"
               for m in _builtin_definitions())


def test_provenance_class_is_mandatory_and_never_mixed():
    validate_statement(statement())
    with pytest.raises(FoodCompositionError):
        validate_statement(statement(provenance_class="reference"))
    missing = statement()
    del missing["provenance_class"]
    with pytest.raises(FoodCompositionError):
        validate_statement(missing)
    with pytest.raises(FoodCompositionError):  # no scores or advice, ever
        validate_statement(statement(source_fields={"nutriscore": "a"}))


def test_units_stay_as_published_and_unknown_stays_unknown():
    assert normalize_unit("MG") == {"published": "MG", "normalized": "mg", "state": "known"}
    assert normalize_unit("XYZ") == {"published": "XYZ", "normalized": None, "state": "unknown"}
    assert normalize_unit(None)["state"] == "absent"
    bad = statement()
    bad["nutrient_values"][0]["unit"] = {"published": "XYZ", "normalized": "g", "state": "unknown"}
    with pytest.raises(FoodCompositionError):
        validate_statement(bad)


def test_gtin_normalisation_pads_and_checks():
    assert gtin_key("071000000208") == gtin_key("0071000000208") == gtin_key("00071000000208") == "00071000000208"
    assert gtin_key("4000000000105") == "04000000000105"
    assert gtin_key("4000000000106") is None  # wrong check digit: never matched


def test_label_revisions_are_immutable_and_ordered_by_the_provider():
    conn = duckdb.connect(":memory:")
    clock = iter(range(1000, 10**9, 1000))
    store = FoodCompositionStore(conn, now=lambda: next(clock))
    first = store.apply(NS, statement())
    assert first["status"] == "created"
    assert store.apply(NS, statement())["status"] == "unchanged"  # replay adds nothing
    newer = statement(revision={"value": "5", "basis": "off-revision", "date": "2026-02-01", "declared": None},
                      allergen_declarations=[])
    assert store.apply(NS, newer)["status"] == "revised"
    late = statement(revision={"value": "4", "basis": "off-revision", "date": "2026-01-20", "declared": None})
    assert store.apply(NS, late)["status"] == "history"  # older payload delivered late never replaces current
    revisions = store.revisions(NS, first["food_id"])
    assert [r["revision_value"] for r in revisions] == ["3", "5", "4"]
    assert store.current_revision(NS, first["food_id"])["revision_value"] == "5"
    # The first revision's parts are untouched by later ones.
    assert store.parts(NS, first["revision_id"])["allergen_declarations"][0]["value"] == "en:milk"
    from datetime import date

    chosen, _ = store.revision_as_of(NS, first["food_id"], date(2026, 1, 25))
    assert chosen["revision_value"] == "4"


def test_every_record_type_round_trips_with_its_envelope():
    conn = duckdb.connect(":memory:")
    store = FoodCompositionStore(conn, now=lambda: 1_700_000_000_000)
    applied = store.apply(NS, statement())
    records = store.export_records(NS, applied["revision_id"])
    assert {r["record_type"] for r in records} == set(RECORD_TYPES) - {"generic-food"}
    for record in records:
        assert record["provenance_class"] == "crowd-sourced"
        assert record["attribution"]["licence"] == "ODbL-1.0" and record["attribution"]["share_alike"] is True
        assert record["retrieved_at"] == "2023-11-14T22:13:20Z" and record["as_of"] == "2026-01-10"
        assert record["revision"]["value"] == "3" and record["provider_key"] == "off:4000000000150"
    label = next(r for r in records if r["record_type"] == "label-revision")
    # The label revision carries the statement unchanged: re-applying it adds nothing.
    assert store.apply(NS, copy.deepcopy(label["body"]["statement"]))["status"] == "unchanged"
    generic = statement(provider="composition-table", provider_key="ciqual:13039", food_kind="generic-food",
                        provenance_class="reference",
                        revision={"value": "2020", "basis": "table-edition", "date": "2020-07-07",
                                  "declared": "2020"},
                        attribution={"licence": "Licence Ouverte / Etalab 2.0", "text": "Anses", "share_alike": False})
    records = store.export_records(NS, store.apply(NS, generic)["revision_id"])
    assert "generic-food" in {r["record_type"] for r in records}
