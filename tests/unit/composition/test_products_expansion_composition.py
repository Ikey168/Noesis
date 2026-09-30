"""The Products bundle's optional ``appliances`` and ``components`` features (PX10, #2102)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.products import products_feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.products import (
    EXPANSION_READS,
    EXPANSION_TOOLS,
    PRODUCT_SCOPES,
)

ROOT = Path(__file__).resolve().parents[3]
APPLIANCE_PROVIDERS = {
    "products.core",
    "products.appliances",
    "platform.source-runtime",
}
COMPONENT_PROVIDERS = {
    "products.core",
    "products.appliances",
    "products.components",
    "platform.entity-identity",
    "platform.source-runtime",
}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (
        dict(domain_registry._REGISTRY),
        set(domain_registry._ENABLED),
        domain_registry._AUTHORITY,
    )
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def products_plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "products", "version": bundles["products"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve(
        [root],
        list(bundles.values()),
        descriptors if descriptors is not None else provider_descriptors(),
    )
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "products" in b["consumers"]}


def test_descriptors_declare_capabilities_operations_stores_and_the_source_pack():
    descriptors = {
        d["id"]: d for d in provider_descriptors() if d["id"].startswith("products.")
    }
    assert set(descriptors) == {
        "products.core",
        "products.safety",
        "products.appliances",
        "products.components",
        "products.food",  # food composition (#2216)
    }
    catalog = {
        t["name"]: t
        for t in json.loads(
            (ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
        )["tools"]
    }
    declared = set()
    for provider in ("products.appliances", "products.components"):
        descriptor = descriptors[provider]
        assert validate_provider_descriptor(descriptor) == []
        assert descriptor["source_packs"] == [
            {"pack_id": "products-displays", "version": "1.2.0", "range": "^1.2.0"}
        ]
        for operation in descriptor["operations"]:
            name = operation["tool"].split(".", 1)[1]
            declared.add(name)
            # Exactly the scopes the tool declares in the catalog, and the catalog lists it.
            assert (
                operation["required_scopes"]
                == PRODUCT_SCOPES[name]
                == catalog[name]["required_scopes"]
            ), name
            assert (operation["side_effect"] == "read-only") == (
                name in EXPANSION_READS
            ), name
    assert declared == EXPANSION_TOOLS
    stores = [s for d in descriptors.values() for s in d["stores"]]
    assert len({s["record_type"] for s in stores}) == len(
        stores
    )  # one authority per record type
    # src.kb.products stays the one record owner: the new tables are products.core stores.
    assert (
        descriptors["products.appliances"]["stores"]
        == descriptors["products.components"]["stores"]
        == []
    )
    core = {
        s["record_type"]: s["tables"] for s in descriptors["products.core"]["stores"]
    }
    assert core["product-lifecycle-status"] == ["product_lifecycle"]
    assert core["product-manufacturer-link"] == ["product_manufacturer_links"]
    assert "product_document_pages" in core["product"]
    # Existing pins stay inside their ranges.
    assert descriptors["products.core"]["source_packs"][0]["range"] == "^1.0.0"
    assert descriptors["products.safety"]["source_packs"][0]["range"] == "^1.1.0"


def test_features_are_off_by_default_and_the_v1_manifest_is_unchanged():
    composition = json.loads((ROOT / "packs/products/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert {k: v["default"] for k, v in features.items()} == {
        "safety": False,
        "appliances": False,
        "components": False,
        "food": False,
    }
    assert {r["capability"] for r in features["components"]["requires"]} == {
        "products.components",
        "products.categories",
        "products.identities",
        "platform.entity-identity",
        "platform.source-acquisition",
    }
    assert validate_composition_manifest(adapt_all()["products"]) == []
    plan = products_plan()
    assert plan["features"]["products"] == [] and bound(plan) == {"products.core"}
    assert {
        "pack_id": "products-displays",
        "version": "1.3.0",  # the bundle pins 1.3.0 (food sources, #2216) inside its ^1.0.0 range
        "range": "^1.0.0",
    } in plan["source_packs"]
    pack = json.loads((ROOT / "packs/products/pack.json").read_text())
    assert (
        pack["version"] == "1.0.0"
        and pack["schema_versions"]["product-record"] == "1.0.0"
    )
    assert not any(
        word in json.dumps(pack).lower()
        for word in ("appliance", "bmecat", "component")
    )


@pytest.mark.parametrize(
    ("features", "providers"),
    [
        (["appliances"], APPLIANCE_PROVIDERS),
        (["components"], COMPONENT_PROVIDERS),
        (["appliances", "components"], COMPONENT_PROVIDERS),
    ],
)
def test_selecting_a_feature_binds_its_providers(features, providers):
    plan = products_plan(features)
    assert plan["features"]["products"] == sorted(features) and bound(plan) == providers
    bindings = [b for b in plan["bindings"] if "products" in b["consumers"]]
    assert len({b["capability"] for b in bindings}) == len(
        bindings
    )  # one provider per capability
    omitted = {o["feature"] for o in plan["omissions"] if o["pack"] == "products"}
    assert omitted == {"appliances", "components", "food", "safety"} - set(features)


def test_the_expansion_composes_with_the_safety_feature():
    plan = products_plan(["appliances", "components", "safety"])
    assert plan["features"]["products"] == ["appliances", "components", "safety"]
    assert {"products.safety", "products.appliances", "products.components"} <= bound(
        plan
    )
    assert [o for o in plan["omissions"] if o["pack"] == "products"] == [
        {"pack": "products", "feature": "food", "reason": "not selected"}]


def test_a_missing_consumed_provider_is_a_visible_omission_not_a_failure():
    descriptors = [
        d for d in provider_descriptors() if d["id"] != "platform.entity-identity"
    ]
    plan = products_plan(["appliances", "components"], descriptors)
    assert plan["features"]["products"] == ["appliances"]
    omission = next(o for o in plan["omissions"] if o["feature"] == "components")
    assert (
        omission["capability"] == "platform.entity-identity"
        and "missing_contract" in omission["reason"]
    )


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert not products_feature_enabled(
        conn, "appliances"
    ) and not products_feature_enabled(conn, "components")
    coordinator.select(
        "products", bundles["products"]["version"], features=["components"]
    )
    assert coordinator.activate("products-components-on")["status"] == "published"
    assert products_feature_enabled(
        conn, "components"
    ) and not products_feature_enabled(conn, "appliances")
    coordinator.select("products", bundles["products"]["version"], features=[])
    coordinator.activate("products-components-off")
    assert not products_feature_enabled(conn, "components")
