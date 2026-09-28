"""Category-scoped attribute registry (PX02 #2094) and component identity records (PX05 #2097).

The display regression replays the products-displays fixtures and compares
normalised values, match candidates and a comparison with the snapshot taken
before the registry existed (``tests/fixtures/products/display-regression.json``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.ingestion import product_sources
from src.kb import product_categories as registry
from src.kb.products import ProductError, ProductStore
from src.kb.schema_registry import READ_SCOPE as SCHEMA_READ
from src.kb.schema_registry import SchemaRegistry
from tests.unit.domains import test_products_pack as displays

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = ROOT / "tests/fixtures/products/display-regression.json"


@pytest.fixture()
def store():
    conn = duckdb.connect(":memory:")
    yield ProductStore(conn)
    conn.close()


def normalize(store, category, attribute, value, unit=None, scheme=None):
    return store._normalize(
        "global",
        {
            "attribute": attribute,
            "native_value": value,
            "native_unit": unit,
            "label_scheme": scheme,
        },
        category,
    )


# ------------------------------------------------------------------ registry


def test_registry_is_consistent_and_lists_the_bounded_categories():
    assert registry.validate_registry() == []
    assert registry.categories() == [
        "electronic-displays",
        "household-washing-machines",
        "multilayer-ceramic-capacitors",
        "refrigerating-appliances",
        "thick-film-chip-resistors",
    ]
    assert registry.categories("component") == [
        "multilayer-ceramic-capacitors",
        "thick-film-chip-resistors",
    ]
    assert (
        registry.resolve_label("Household Washing Machines")
        == "household-washing-machines"
    )
    assert registry.resolve({"label": "electronic displays"}) == "electronic-displays"
    assert (
        registry.resolve_label("washing machines") is None
    )  # never a guess from similarity
    assert (
        registry.eprel_category_for_group("refrigeratingappliances2019")
        == "refrigerating-appliances"
    )
    for category in registry.categories():
        spec = registry.category(category)
        assert spec["comparison"]["notice"] and spec["matching"]["corroboration"]
    schemes = registry.label_schemes("household-washing-machines")
    assert schemes["EU_2019_2014"]["regulation"].startswith(
        "Commission Delegated Regulation (EU) 2019/2014"
    )


def test_registry_validation_names_every_broken_reference():
    broken = registry.registry()
    washing = broken["household-washing-machines"]
    washing["attributes"]["rated_capacity"]["canonical_units"] = ["stone"]
    washing["comparison"]["attributes"].append("colour")
    washing["matching"]["corroboration"].append(
        {"attribute": "colour", "rule": "similar"}
    )
    washing["providers"]["icecat"]["features"].append(["colour", "colour", None])
    broken["refrigerating-appliances"]["labels"] = ["household washing machines"]
    broken["multilayer-ceramic-capacitors"]["identity"] = "brand-designation"
    errors = "\n".join(registry.validate_registry(broken))
    for fragment in (
        "unregistered unit 'stone'",
        "comparison names unregistered attribute colour",
        "corroboration rule 'similar' is unknown",
        "icecat maps to unregistered attribute colour",
        "is also used by",
        "components (and only components) use manufacturer-mpn identity",
    ):
        assert fragment in errors, fragment


def test_display_mappings_are_the_registry_entry_unchanged():
    # The pinned display contract keeps its names and order (record order feeds the fixture output hash).
    assert list(product_sources.EPREL_FIELDS) == [
        "diagonalCm",
        "diagonalInch",
        "resolutionHorizontalPixels",
        "resolutionVerticalPixels",
        "powerOnModeSDR",
        "powerOnModeHDR",
        "energyConsumption1000hSDR",
        "energyConsumption1000hHDR",
        "energyClassSDR",
        "energyClassHDR",
    ]
    assert product_sources.EPREL_FIELDS["powerOnModeHDR"] == (
        "on_mode_power",
        "hdr",
        "W",
    )
    assert product_sources.ICECAT_FEATURES["display diagonal"] == ("diagonal", None)
    assert product_sources._ICECAT_UNITS == {
        '"': "in",
        "inch": "in",
        "cm": "cm",
        "mm": "mm",
        "w": "W",
        "kwh": "kWh",
    }


# ------------------------------------------------------------ display regression


def test_display_normalisation_matches_and_comparison_are_identical_to_the_snapshot():
    expected = json.loads(SNAPSHOT.read_text())
    conn = duckdb.connect(":memory:")
    try:
        value, runtime = displays.install(conn)
        displays.run(runtime, value, "fixture-1")
        store = ProductStore(conn)
        variants = store.lookup("global", scopes=displays.SCOPES, limit=100)["variants"]
        name = {v["model_id"]: f"{v['provider']}:{v['designation']}" for v in variants}
        normalized = sorted(
            (
                [
                    v["provider"],
                    v["designation"],
                    v["provider_record_id"],
                    a["attribute"],
                    a["mode"],
                    a["native_name"],
                    a["native_value"],
                    a["native_unit"],
                    a["normalized_value"],
                    a["normalized_unit"],
                    a["normalization_state"],
                    a["conditions"].get("label_scheme"),
                    a["conditions"].get("normalization_version"),
                    a["locator"],
                ]
                for v in variants
                for a in store.assertions("global", v["variant_id"])
            ),
            key=json.dumps,
        )
        assert normalized == expected["normalized"]
        candidates = store.propose_matches(
            "global", scopes=displays.SCOPES, principal_id="m"
        )["candidates"]
        matches = sorted(
            (
                [
                    name[c["left_model_id"]],
                    name[c["right_model_id"]],
                    c["candidate_state"],
                    c["confidence"],
                    c["evidence"],
                    c["reasons"],
                    c["method"],
                ]
                for c in candidates
            ),
            key=json.dumps,
        )
        assert matches == expected["matches"]
        assert {c["category_id"] for c in candidates} == {"electronic-displays"}
        displays.accept_proposed(store)
        models = [
            displays.variant(store, "icecat", d)["model_id"]
            for d in ("EX-27Q4", "EX-32U8", "EX-24F1")
        ]
        result = store.compare("global", models, scopes=displays.SCOPES)
        rows = [
            [
                r["attribute"],
                r["mode"],
                r["comparable"],
                r["not_comparable_reason"],
                [
                    [
                        c["state"],
                        c["value"],
                        sorted(
                            (
                                [
                                    x["provider"],
                                    x["native_value"],
                                    x["native_unit"],
                                    x["normalized_value"],
                                    x["normalized_unit"],
                                    x["normalization_state"],
                                    x["label_scheme"],
                                    x["evidence"]["locator"],
                                ]
                                for x in c["values"]
                            ),
                            key=json.dumps,
                        ),
                    ]
                    for c in r["cells"]
                ],
            ]
            for r in result["rows"]
        ]
        assert rows == expected["comparison"]["rows"]
        assert result["notice"] == expected["comparison"]["notice"]
        assert [len(c["members"]) for c in result["columns"]] == expected["comparison"][
            "members"
        ]
        assert result["category"] == "electronic-displays"
        assert all(
            "tolerance" not in cell for row in result["rows"] for cell in row["cells"]
        )
    finally:
        conn.close()


# ----------------------------------------------------------------- normalisation


def test_units_are_category_aware_and_convert_exactly(store):
    washing = "household-washing-machines"
    per_cycle = normalize(
        store, washing, "energy_consumption_100_cycles", "0.54", "kWh/cycle"
    )
    assert (per_cycle["state"], per_cycle["value"], per_cycle["unit"]) == (
        "normalized",
        "54.00",
        "kWh/100cycles",
    )
    assert per_cycle["calculation_id"]
    # A plain energy is not a per-100-cycles energy, and a category refuses units it does not accept.
    assert (
        normalize(store, washing, "energy_consumption_100_cycles", "49", "kWh")["state"]
        == "unit_not_allowed"
    )
    assert (
        normalize(store, washing, "rated_capacity", "8", "L")["state"]
        == "unit_not_allowed"
    )
    assert (
        normalize(
            store,
            "refrigerating-appliances",
            "annual_energy_consumption",
            "212",
            "kWh/annum",
        )["value"]
        == "212"
    )
    mlcc = "multilayer-ceramic-capacitors"
    assert normalize(store, mlcc, "capacitance", "4.7", "uF")["value"] == "4700000.000"
    assert (
        normalize(store, mlcc, "capacitance", "0.1", "µF")["state"]
        == "unit_not_allowed"
    )  # accepted symbols only
    assert normalize(store, mlcc, "capacitance", "100", "nF")["value"] == "100000.000"
    assert (
        normalize(store, mlcc, "rated_voltage", "50", "F")["state"]
        == "unit_not_allowed"
    )
    assert (
        normalize(store, "thick-film-chip-resistors", "resistance", "4.7", "kohm")[
            "value"
        ]
        == "4700.000"
    )
    assert (
        normalize(store, "thick-film-chip-resistors", "resistance", "10", "mohm")[
            "value"
        ]
        == "0.010"
    )
    assert (
        normalize(store, mlcc, "capacitance", "about", "pF")["state"] == "unparseable"
    )
    assert normalize(store, mlcc, "capacitance", "100", None)["state"] == "unit_unknown"


def test_tolerances_enums_codes_and_label_classes(store):
    mlcc = "multilayer-ceramic-capacitors"
    assert (
        normalize(store, mlcc, "capacitance_tolerance", "±10", "percent")["value"]
        == "10.00"
    )
    assert (
        normalize(store, mlcc, "capacitance_tolerance", "+/-0.25", "pF")["value"]
        == "0.25"
    )
    absolute = normalize(store, mlcc, "capacitance_tolerance", "±0.25", "pF")
    assert (absolute["value"], absolute["unit"]) == (
        "0.25",
        "pF",
    )  # the canonical unit follows the dimension
    assert (
        normalize(store, mlcc, "capacitance_tolerance", "+80/-20", "percent")["state"]
        == "asymmetric_tolerance"
    )
    assert (
        normalize(store, mlcc, "capacitance_tolerance", "-20", "percent")["state"]
        == "asymmetric_tolerance"
    )
    assert normalize(store, mlcc, "dielectric", "np0")["value"] == "C0G"
    assert normalize(store, mlcc, "dielectric", "X9Q")["state"] == "not_in_vocabulary"
    assert normalize(store, mlcc, "package", " 0603 ")["value"] == "0603"
    fridge = "refrigerating-appliances"
    assert (
        normalize(store, fridge, "climate_class", ["T", "sn", "N"])["value"] == "N,SN,T"
    )
    assert (
        normalize(store, fridge, "climate_class", "SN-T")["state"]
        == "not_in_vocabulary"
    )  # ranges not expanded
    washing = "household-washing-machines"
    assert (
        normalize(store, washing, "energy_class", "a", scheme="EU_2019_2014")["value"]
        == "A"
    )
    assert (
        normalize(store, washing, "energy_class", "A+++", scheme="EU_2019_2014")[
            "state"
        ]
        == "not_in_label_scheme"
    )
    assert (
        normalize(store, washing, "energy_class", "A+++")["value"] == "A+++"
    )  # no scheme named: generic classes
    assert (
        normalize(store, washing, "noise_class", "E", scheme="EU_2019_2014")["state"]
        == "not_in_label_scheme"
    )
    assert normalize(store, washing, "colour", "white")["state"] == "not_normalized"
    assert (
        normalize(store, None, "rated_capacity", "8", "kg")["state"]
        == "category_unregistered"
    )
    # Displays keep the display rules when no category is passed.
    assert (
        store._normalize(
            "global",
            {"attribute": "diagonal", "native_value": "27", "native_unit": "in"},
        )["value"]
        == "68.6"
    )


# ------------------------------------------------------------------ components


def test_component_identity_normalises_both_sides_the_same_way():
    assert registry.mpn_key(" cx0603x7r104k500 ") == "CX0603X7R104K500"
    assert registry.mpn_key("LM317T/NOPB") != registry.mpn_key(
        "LM317T"
    )  # suffixes name different parts
    assert registry.mpn_key("n/a") is None and registry.mpn_key("") is None
    assert (
        registry.manufacturer_key("Capatronic GmbH")
        == registry.manufacturer_key("CAPATRONIC")
        == "capatronic"
    )
    assert registry.manufacturer_key("The Othercap Co., Ltd.") == "othercap"
    assert (
        registry.manufacturer_key("Group") == "group"
    )  # a name is never reduced to nothing
    assert registry.component_key("Capatronic GmbH", "cx 0603") == "capatronic|CX0603"
    assert registry.component_key(None, "CX0603") is None


def test_lifecycle_status_is_the_published_text_mapped_never_inferred():
    assert registry.lifecycle_status("NRND") == {"declared": "NRND", "value": "nrnd"}
    assert (
        registry.lifecycle_status("Not recommended for new designs")["value"] == "nrnd"
    )
    assert registry.lifecycle_status("Last  Time Buy") == {
        "declared": "Last Time Buy",
        "value": "last-time-buy",
    }
    assert registry.lifecycle_status("End of life 2027")["value"] == "unknown"
    assert (
        registry.lifecycle_status("n/a") is None
        and registry.lifecycle_status(None) is None
    )


def test_unregistered_category_arguments_are_explicit_errors(store):
    with pytest.raises(ProductError) as caught:
        store.lookup("global", scopes=displays.SCOPES, category="toasters")
    assert caught.value.code == "unknown_category"


# ------------------------------------------------------------------- schema


def test_product_record_schema_1_1_0_is_registered_beside_1_0_0():
    conn = duckdb.connect(":memory:")
    registry_store = SchemaRegistry(conn)
    old = registry_store.resolve(
        "schema", "product-record", "1.0.0", scopes={SCHEMA_READ}
    )
    new = registry_store.resolve(
        "schema", "product-record", "^1.0.0", scopes={SCHEMA_READ}
    )
    assert new["semantic_version"] == "1.1.0"
    assert (
        SchemaRegistry.compare_content(old["content"], new["content"])["classification"]
        != "breaking"
    )
    displays_value = displays.manifest()
    for source_id, cls in (
        ("icecat-displays", product_sources.IcecatProductAdapter),
        ("eprel-displays", product_sources.EprelProductAdapter),
    ):
        item = copy.deepcopy(displays.source(displays_value, source_id))
        adapter = cls(
            item,
            transport=product_sources.fixture_transport(displays.pages(source_id)),
            secret="fixture-credential",
        )
        record = adapter.fetch_page(
            {"operation": "models", "parameters": {}}, cursor=None
        ).records[0]
        for schema in (old["content"], new["content"]):
            assert not list(
                jsonschema.Draft7Validator(schema).iter_errors(record["product_record"])
            )
    conn.close()
