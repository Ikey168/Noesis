"""The osint.movements provider and the Osint bundle's optional movements feature (#2285, MV13)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def osint_plan(features=None):
    bundles = adapt_all()
    root = {"pack": "osint", "version": bundles["osint"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def test_descriptor_validates_owns_the_movement_store_and_pins_the_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "osint.movements")
    assert validate_provider_descriptor(descriptor) == []
    assert (ROOT / "packs/osint/providers/osint.movements.json").exists()
    (capability,) = descriptor["capabilities"]
    assert capability["id"] == "osint.movements"
    constraints = capability["semantic_constraints"]
    assert constraints["optional_feature"] == "movements" and constraints["default_enabled"] is False
    assert "NOESIS_OSINT_MOVEMENTS" in constraints["feature_flag"]
    assert all(op["side_effect"] == "read-only" for op in descriptor["operations"])
    (store,) = descriptor["stores"]
    assert store["store"] == "src.osint.movements" and "osint_movement_revisions" in store["tables"]
    assert descriptor["source_packs"] == [{"pack_id": "bounded-public-osint", "version": "1.1.0", "range": "^1.1.0"}]


def test_movements_feature_is_off_by_default_and_binds_the_provider_when_selected():
    composition = json.loads((ROOT / "packs/osint/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert features["movements"]["default"] is False
    assert validate_composition_manifest(adapt_all()["osint"]) == []
    default = osint_plan()
    bound = {b["capability"] for b in default["bindings"]}
    assert "osint.movements" not in bound
    assert "movements" in {o["feature"] for o in default["omissions"] if o["pack"] == "osint"}
    selected = osint_plan(["movements"])
    capabilities = {b["capability"] for b in selected["bindings"]}
    assert {"osint.movements", "ownership.identity", "platform.subscriptions"} <= capabilities


def test_pack_manifest_records_the_capability_and_the_tracking_exclusion():
    pack = json.loads((ROOT / "packs/osint/pack.json").read_text())
    assert "bounded-aircraft-and-vessel-movements" in pack["capabilities"]
    assert pack["schema_versions"]["osint-movement-record"] == "1.0.0"
    assert any("real-time tracking" in e for e in pack["exclusions"])
    source_pack = json.loads((ROOT / "config/source_packs/osint.json").read_text())
    assert source_pack["version"] == "1.2.0"  # 1.2.0 adds the News fact-checks sources (#2659)
    assert {s["connector"] for s in source_pack["sources"] if s["source_id"].startswith("movements-")} == {
        "osint-movements"}
