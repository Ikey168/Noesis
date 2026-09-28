"""The Products bundle's optional ``safety`` feature and the ``products.safety`` provider (R10, #2026)."""

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
from src.kb.product_safety import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.products import (
    PRODUCT_SCOPES,
    SAFETY_READS,
    SAFETY_TOOLS,
)

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "products.core",
    "products.safety",
    "technology.standards",
    "legal.core",
    "news.core",
    "platform.entity-identity",
    "platform.subscriptions",
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


def test_descriptor_declares_the_capability_operations_stores_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "products.safety")
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {
        "name": "src.kb.product_safety",
        "version": "1.0.0",
        "server": "noesis-knowledge-engine",
    }
    capability = descriptor["capabilities"][0]
    assert capability["id"] == "products.safety-notices"
    assert capability["contract"] == {
        "name": "noesis-product-safety-notice",
        "version": "1.0.0",
    }
    tools = {o["tool"].split(".", 1)[1]: o for o in descriptor["operations"]}
    assert (
        {
            "lookup_product_notices",
            "inspect_product_notice",
            "propose_product_notice_matches",
            "review_product_notice_match",
        }
        <= set(tools)
        <= SAFETY_TOOLS
    )
    for name, operation in tools.items():
        # Preserved tool ids with exactly the scopes the tool declares in the catalog.
        assert operation["required_scopes"] == PRODUCT_SCOPES[name], name
        assert (operation["side_effect"] == "read-only") == (name in SAFETY_READS), name
    assert {s["store"] for s in descriptor["stores"]} == {"src.kb.product_safety"}
    assert all(
        s["revision_addressable"] and s["namespace_scoped"]
        for s in descriptor["stores"]
    )
    assert "product_safety_revisions" in descriptor["stores"][0]["tables"]
    assert descriptor["readiness_probes"] == [
        {
            "id": "revisions",
            "kind": "table-exists",
            "target": "product_safety_revisions",
        }
    ]
    # products-displays 1.2.0 (#2061) adds sources inside the safety provider's ^1.1.0 range.
    assert descriptor["source_packs"] == [
        {"pack_id": "products-displays", "version": "1.2.0", "range": "^1.1.0"}
    ]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "products.safety"
        for s in d["stores"]
    }
    assert not owned & others
    catalog = json.loads(
        (ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    listed = {t["name"]: t for t in catalog["tools"] if t["name"] in SAFETY_TOOLS}
    assert set(listed) == SAFETY_TOOLS
    assert all(listed[n]["required_scopes"] == PRODUCT_SCOPES[n] for n in SAFETY_TOOLS)


def test_the_bundle_resolves_with_the_feature_off_by_default():
    composition = json.loads((ROOT / "packs/products/composition.json").read_text())
    # The expansion's appliances and components features (#2061) sit beside safety, all off by default.
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(features) == {"safety", "appliances", "components"}
    assert not any(f["default"] for f in features.values())
    feature = features["safety"]
    assert {r["capability"] for r in feature["requires"]} == {
        "products.safety-notices",
        "products.identities",
        "technology.standards",
        "legal.works",
        "news.articles",
        "platform.entity-identity",
        "platform.subscriptions",
        "platform.source-acquisition",
    }
    assert validate_composition_manifest(adapt_all()["products"]) == []
    plan = products_plan()
    assert plan["features"]["products"] == [] and bound(plan) == {"products.core"}
    assert {"pack": "products", "feature": "safety", "reason": "not selected"} in plan[
        "omissions"
    ]
    assert {
        "pack_id": "products-displays",
        "version": "1.2.0",
        "range": "^1.0.0",
    } in plan["source_packs"]


def test_selecting_the_feature_binds_one_authority_per_store_and_every_requirement():
    plan = products_plan(["safety"])
    assert (
        plan["features"]["products"] == ["safety"] and bound(plan) == FEATURE_PROVIDERS
    )
    # The consumed bundles keep their own optional features unselected; nothing of safety is omitted, and the
    # bundle's other features (appliances, components; #2061) stay unselected.
    assert [o for o in plan["omissions"] if o["pack"] == "products"] == [
        {"pack": "products", "feature": "appliances", "reason": "not selected"},
        {"pack": "products", "feature": "components", "reason": "not selected"},
    ]
    bindings = [b for b in plan["bindings"] if "products" in b["consumers"]]
    assert len({b["capability"] for b in bindings}) == len(
        bindings
    )  # one provider per capability
    assert {"pack_id": "legal-research", "version": "1.3.0", "range": "^1.1.0"} in plan[
        "source_packs"
    ]
    stores = [
        s["store"]
        for d in provider_descriptors()
        if d["id"] in FEATURE_PROVIDERS
        for s in d["stores"]
    ]
    # src.kb.products holds the product, lifecycle-status and manufacturer-link record types (#2061), all owned
    # by products.core.
    assert (
        stores.count("src.kb.product_safety") == 2
        and stores.count("src.kb.products") == 3
    )


def test_a_missing_consumed_provider_is_a_visible_omission_not_a_failure():
    descriptors = [
        d for d in provider_descriptors() if d["id"] != "technology.standards"
    ]
    plan = products_plan(["safety"], descriptors)
    assert plan["features"]["products"] == [] and bound(plan) == {"products.core"}
    omission = next(o for o in plan["omissions"] if o["feature"] == "safety")
    assert (
        omission["capability"] == "technology.standards"
        and "missing_contract" in omission["reason"]
    )


def test_the_v1_pack_manifest_is_unchanged():
    pack = json.loads((ROOT / "packs/products/pack.json").read_text())
    assert pack["version"] == "1.0.0" and "safety" not in json.dumps(pack).lower()
    assert not list(ROOT.glob("packs/*safety*"))


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("products", bundles["products"]["version"], features=["safety"])
    assert coordinator.activate("products-safety-on")["status"] == "published"
    assert feature_enabled(conn) is True
    coordinator.select("products", bundles["products"]["version"], features=[])
    coordinator.activate("products-safety-off")
    assert feature_enabled(conn) is False
