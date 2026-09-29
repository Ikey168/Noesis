"""The Engineering Safety bundle and its providers under composition (ES15, #2076)."""

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
from src.domains.pack_format import validate_manifest
from src.ingestion.source_pack_runtime import PROJECTORS
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
OWN = {
    "engineering-safety.directives",
    "engineering-safety.investigations",
    "engineering-safety.recommendations",
    "engineering-safety.defects",
    "engineering-safety.identity",
}
SHARED = {
    "platform.source-runtime",
    "platform.subscriptions",
    "platform.entity-identity",
    "technology.standards",
    "legal.core",
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


def plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {
        "pack": "engineering-safety",
        "version": bundles["engineering-safety"]["version"],
    }
    if features is not None:
        root["features"] = features
    result = resolve(
        [root],
        list(bundles.values()),
        descriptors if descriptors is not None else provider_descriptors(),
    )
    assert result.ok, result.failure
    return result.plan


def bound(value):
    return {
        b["provider"]
        for b in value["bindings"]
        if "engineering-safety" in b["consumers"]
    }


def test_pack_manifest_and_descriptors_are_valid_with_one_owner_per_store():
    pack = json.loads((ROOT / "packs/engineering-safety/pack.json").read_text())
    assert (
        validate_manifest(pack) == []
        and pack["source_pack"] == "config/source_packs/engineering-safety.json"
    )
    assert pack["exclusions"] and all(e["semantics"] for e in pack["query_examples"])
    assert validate_composition_manifest(adapt_all()["engineering-safety"]) == []
    descriptors = {
        d["id"]: d
        for d in provider_descriptors()
        if d["id"].startswith("engineering-safety.")
    }
    assert set(descriptors) == OWN
    for descriptor in descriptors.values():
        assert validate_provider_descriptor(descriptor) == [], descriptor["id"]
    stores = [s["store"] for d in provider_descriptors() for s in d["stores"]]
    assert (
        stores.count("src.kb.engineering_safety_store") == 2
    )  # one provider owns both record types
    owners = [
        d["id"]
        for d in provider_descriptors()
        if any(s["store"] == "src.kb.engineering_safety_store" for s in d["stores"])
    ]
    assert owners == ["engineering-safety.directives"]
    assert descriptors["engineering-safety.directives"]["source_packs"] == [
        {"pack_id": "engineering-safety", "version": "1.0.0", "range": "^1.0.0"}
    ]
    assert "noesis-engineering-safety-record-v1" in PROJECTORS


def test_the_bundle_resolves_with_its_optional_features_off_by_default():
    value = plan()
    assert bound(value) == OWN | SHARED
    assert value["features"]["engineering-safety"] == []
    omitted = {
        o["feature"] for o in value["omissions"] if o["pack"] == "engineering-safety"
    }
    assert omitted == {"products", "ownership", "places"}


def test_selecting_the_features_binds_products_ownership_and_geospatial_owners():
    value = plan(["products", "ownership", "places"])
    assert bound(value) >= OWN | SHARED | {
        "products.core",
        "products.safety",
        "ownership.core",
        "geospatial.core",
    }
    bindings = [b for b in value["bindings"] if "engineering-safety" in b["consumers"]]
    assert len({b["capability"] for b in bindings}) == len(bindings)


def test_a_missing_consumed_provider_is_a_visible_omission_not_a_failure():
    descriptors = [d for d in provider_descriptors() if d["id"] != "products.safety"]
    value = plan(["products"], descriptors)
    assert "products" not in value["features"]["engineering-safety"]
    omission = next(o for o in value["omissions"] if o.get("feature") == "products")
    assert omission["capability"] == "products.safety-notices"


def test_disabling_engineering_safety_is_a_selection_change_that_keeps_shared_providers():
    conn, coordinator, _, _ = _migrated()
    active = coordinator.active()["plan"]
    assert "engineering-safety" in {p["id"] for p in active["packs"]}
    assert coordinator.deselect("engineering-safety")
    receipt = coordinator.activate("engineering-safety-off")
    assert receipt["status"] == "published"
    after = coordinator.active()["plan"]
    assert "engineering-safety" not in {p["id"] for p in after["packs"]}
    assert "products" in {p["id"] for p in after["packs"]} and "legal" in {
        p["id"] for p in after["packs"]
    }
    from src.kb.engineering_safety_store import EngineeringSafetyStore

    EngineeringSafetyStore(
        conn
    )  # records stay addressable; disabling never deletes evidence
