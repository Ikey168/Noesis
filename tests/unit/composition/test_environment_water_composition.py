"""The ``environment.water`` provider and the Climate and Environment bundle's optional water features (#2582, WA11)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.water import (
    WATER_FEATURES,
    WATER_TOOLS,
    readiness,
    selected_features,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "climate-environment-water", "version": "1.0.0", "range": "^1.0.0"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def plan_for(features=None):
    bundles = adapt_all()
    root = {"pack": "climate-environment", "version": bundles["climate-environment"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "climate-environment" in b["consumers"]}


def test_descriptor_declares_capabilities_constraints_stores_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "environment.water")
    assert validate_provider_descriptor(descriptor) == []
    assert {c["id"] for c in descriptor["capabilities"]} == {
        "environment.water-records", "environment.water-identity", "environment.water-monitoring"}
    constraints = descriptor["capabilities"][0]["semantic_constraints"]
    assert {"observations", "assessments", "minimisation", "exclusions"} <= set(constraints)
    assert "no flood forecasting" in constraints["exclusions"] and "gap filling" in constraints["exclusions"]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "environment.water"
              for s in d["stores"]}
    assert not owned & others
    assert {o["tool"].split(".", 1)[1] for o in descriptor["operations"]} == WATER_TOOLS


def test_three_optional_features_off_by_default_leave_the_bundle_unchanged():
    manifest = json.loads((ROOT / "packs/climate-environment/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    features = {f["id"]: f for f in manifest["optional_features"]}
    assert set(WATER_FEATURES.values()) <= set(features)
    assert {features[f]["default"] for f in WATER_FEATURES.values()} == {False}
    assert not any(r["capability"].startswith("environment.water") for r in manifest["requires"])
    for feature in WATER_FEATURES.values():
        required = {r["capability"] for r in features[feature]["requires"]}
        assert not required & {"hazards.events", "weather.observations", "infrastructure.assets"}
    off = plan_for()
    assert "environment.water" not in bound(off) and PACK not in off["source_packs"]
    for feature in WATER_FEATURES.values():
        assert {"pack": "climate-environment", "feature": feature, "reason": "not selected"} in off["omissions"]
    on = plan_for(["water-usgs"])
    assert "environment.water" in bound(on) and on["source_packs"] == off["source_packs"]
    assert bound(off) <= bound(on) and "platform.entity-identity" in bound(on)
    view = CompositionView(on, provider_descriptors(), adapt_all().values())
    assert view.tools["noesis-knowledge-engine.water_value_at"].provider == "environment.water"
    assert not list(ROOT.glob("packs/*water*"))  # no new pack
    assert (ROOT / "packs/climate-environment/providers/environment.water.json").exists()


def test_feature_selection_follows_the_active_composition_and_readiness_reports_it():
    state = readiness(duckdb.connect(":memory:"), "environment")
    assert {f["selected"] for f in state["features"].values()} == {False}
    assert state["not_implemented"]["grdc"]["decision"] == "not implemented"
    assert state["linked_providers"]["natural-hazards"]["status"] == "unavailable"
    conn, coordinator, bundles, _ = _migrated()
    assert selected_features(conn) == set()
    coordinator.select("climate-environment", bundles["climate-environment"]["version"],
                       features=["water-pegelonline", "water-eea-wise"])
    assert coordinator.activate("climate-environment-water-on")["status"] == "published"
    assert selected_features(conn) == {"water-pegelonline", "water-eea-wise"}
    coordinator.select("climate-environment", bundles["climate-environment"]["version"], features=[])
    coordinator.activate("climate-environment-water-off")
    assert selected_features(conn) == set()
