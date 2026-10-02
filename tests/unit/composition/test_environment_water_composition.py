"""The Climate and Environment bundle's optional water features and the ``environment.water`` provider (#2582, WA11
#2638)."""

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
    FEATURES,
    WATER_TOOLS,
    WATER_WRITES,
    readiness,
    required_scopes,
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


def test_descriptor_declares_capabilities_constraints_scopes_stores_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "environment.water")
    assert validate_provider_descriptor(descriptor) == []
    assert {c["id"] for c in descriptor["capabilities"]} == {
        "environment.water-records", "environment.water-identity", "environment.water-monitoring"}
    constraints = descriptor["capabilities"][0]["semantic_constraints"]
    assert {"quality", "missing", "minimisation", "exclusions", "links"} <= set(constraints)
    assert "flood-risk" in constraints["exclusions"] and "interpolation" in constraints["exclusions"]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "environment.water" for s in d["stores"]}
    assert not owned & others
    assert {o["tool"].split(".", 1)[1] for o in descriptor["operations"]} == WATER_TOOLS
    for operation in descriptor["operations"]:
        name = operation["tool"].split(".", 1)[1]
        mutability = "write" if name in WATER_WRITES else "read"
        assert operation["required_scopes"] == required_scopes(name, mutability), name
    catalog = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    listed = {t["name"]: t for t in catalog["tools"] if t["name"] in WATER_TOOLS}
    assert set(listed) == WATER_TOOLS
    assert all(listed[n]["mutability"] == ("write" if n in WATER_WRITES else "read") for n in listed)


def test_sources_are_separate_optional_features_off_by_default_and_links_are_never_required():
    manifest = json.loads((ROOT / "packs/climate-environment/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    features = {f["id"]: f for f in manifest["optional_features"]}
    assert set(FEATURES.values()) <= set(features)
    for feature in FEATURES.values():
        assert features[feature]["default"] is False
        required = {r["capability"] for r in features[feature]["requires"]}
        assert not {c for c in required if c.startswith(("hazards.", "weather.", "geospatial.infrastructure"))}
    assert not any(r["capability"].startswith("environment.water") for r in manifest["requires"])
    off = plan_for()
    assert "environment.water" not in bound(off) and PACK not in off["source_packs"]
    for feature in FEATURES.values():
        assert {"pack": "climate-environment", "feature": feature, "reason": "not selected"} in off["omissions"]
    for chosen in (["water-pegelonline"], ["water-usgs"], ["water-eea-wise"], sorted(FEATURES.values())):
        on = plan_for(chosen)
        assert "environment.water" in bound(on) and on["source_packs"] == off["source_packs"]
        assert bound(off) <= bound(on) and "platform.entity-identity" in bound(on)
        assert sorted(on["features"]["climate-environment"]) == sorted(chosen)
    view = CompositionView(plan_for(["water-usgs"]), provider_descriptors(), adapt_all().values())
    assert view.tools["noesis-knowledge-engine.water_value_at"].provider == "environment.water"
    assert not list(ROOT.glob("packs/*water*"))  # no new pack


def test_feature_selection_follows_the_active_composition_and_readiness_reports_it():
    empty = readiness(duckdb.connect(":memory:"), "environment")
    assert empty["features"] == {f: False for f in FEATURES.values()}
    assert empty["links"]["hazards"]["status"] == "unavailable"  # degrades, never fails
    conn, coordinator, bundles, _ = _migrated()
    assert selected_features(conn) == []
    coordinator.select("climate-environment", bundles["climate-environment"]["version"], features=["water-usgs"])
    assert coordinator.activate("climate-environment-water-usgs-on")["status"] == "published"
    assert selected_features(conn) == ["water-usgs"]
    coordinator.select("climate-environment", bundles["climate-environment"]["version"], features=[])
    coordinator.activate("climate-environment-water-off")
    assert selected_features(conn) == []


def test_taxonomy_classifies_the_provider_and_the_gap_row_is_gone():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["environment.water"] == {
        "subdomains": ["water-hydrology"], "shapes": ["observations", "statistical-series"], "themes": ["climate"]}
    assert "| `water-hydrology` |" not in (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
