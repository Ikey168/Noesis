"""The Products bundle's optional ``food`` feature and the ``products.food`` provider (FC10, #2292)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.food_composition import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.products import FOOD_READS, FOOD_TOOLS, PRODUCT_SCOPES

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {"products.core", "products.food", "products.safety", "platform.subscriptions",
                     "platform.source-runtime"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
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
    result = resolve([root], list(bundles.values()),
                     descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "products" in b["consumers"]}


def test_descriptor_declares_the_capability_constraints_operations_stores_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "products.food")
    assert validate_provider_descriptor(descriptor) == []
    capability = descriptor["capabilities"][0]
    assert capability["id"] == "products.food-composition"
    assert capability["contract"] == {"name": "noesis-food-composition-record", "version": "1.0.0"}
    assert set(capability["semantic_constraints"]) == {"provenance", "odbl", "revisions", "identity", "notices",
                                                       "exclusions"}
    catalog = {t["name"]: t for t in json.loads(
        (ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())["tools"]}
    names = set()
    for operation in descriptor["operations"]:
        name = operation["tool"].split(".", 1)[1]
        names.add(name)
        assert operation["required_scopes"] == PRODUCT_SCOPES[name] == catalog[name]["required_scopes"], name
        assert (operation["side_effect"] == "read-only") == (name in FOOD_READS), name
    assert names == FOOD_TOOLS
    assert descriptor["source_packs"] == [{"pack_id": "products-displays", "version": "1.3.0", "range": "^1.3.0"}]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "products.food" for s in d["stores"]}
    assert owned == {"food-composition-record", "food-match", "food-notice-link"} and not owned & others


def test_the_feature_is_off_by_default_and_the_bundle_validates():
    composition = json.loads((ROOT / "packs/products/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert features["food"]["default"] is False
    assert {r["capability"] for r in features["food"]["requires"]} == {
        "products.food-composition", "products.identities", "products.safety-notices", "platform.subscriptions",
        "platform.source-acquisition"}
    assert validate_composition_manifest(adapt_all()["products"]) == []
    plan = products_plan()
    assert plan["features"]["products"] == [] and bound(plan) == {"products.core"}
    assert {"pack": "products", "feature": "food", "reason": "not selected"} in plan["omissions"]
    pack = json.loads((ROOT / "packs/products/pack.json").read_text())
    assert pack["schema_versions"]["food-composition-record"] == "1.0.0"
    assert {e["tool"] for e in pack["query_examples"]} >= {"food_composition_as_of", "food_label_history"}


def test_selecting_the_feature_binds_its_providers_one_per_capability():
    plan = products_plan(["food"])
    assert plan["features"]["products"] == ["food"] and bound(plan) == FEATURE_PROVIDERS
    bindings = [b for b in plan["bindings"] if "products" in b["consumers"]]
    assert len({b["capability"] for b in bindings}) == len(bindings)
    assert {o["feature"] for o in plan["omissions"] if o["pack"] == "products"} == {"safety", "appliances",
                                                                                    "components"}
    everything = products_plan(["appliances", "components", "food", "safety"])
    assert not [o for o in everything["omissions"] if o["pack"] == "products"]


def test_a_missing_consumed_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "platform.subscriptions"]
    plan = products_plan(["food"], descriptors)
    assert plan["features"]["products"] == []
    omission = next(o for o in plan["omissions"] if o["feature"] == "food")
    assert omission["capability"] == "platform.subscriptions" and "missing_contract" in omission["reason"]


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("products", bundles["products"]["version"], features=["food"])
    assert coordinator.activate("products-food-on")["status"] == "published"
    assert feature_enabled(conn) is True
    coordinator.select("products", bundles["products"]["version"], features=[])
    coordinator.activate("products-food-off")
    assert feature_enabled(conn) is False
