"""The Climate and Environment bundle's optional ``biodiversity`` feature and the ``environment.biodiversity``
provider (#2220, BD11 #2530)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.biodiversity import BIODIVERSITY_TOOLS, feature_enabled, readiness

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "climate-environment-biodiversity", "version": "1.0.0", "range": "^1.0.0"}


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
    descriptor = next(d for d in provider_descriptors() if d["id"] == "environment.biodiversity")
    assert validate_provider_descriptor(descriptor) == []
    assert {c["id"] for c in descriptor["capabilities"]} == {
        "environment.biodiversity-records", "environment.biodiversity-identity", "environment.biodiversity-monitoring"}
    constraints = descriptor["capabilities"][0]["semantic_constraints"]
    assert {"sensitive_coordinates", "licences", "exclusions"} <= set(constraints)
    assert "distribution modelling" in constraints["exclusions"]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "environment.biodiversity"
              for s in d["stores"]}
    assert not owned & others
    assert {o["tool"].split(".", 1)[1] for o in descriptor["operations"]} == BIODIVERSITY_TOOLS


def test_the_feature_is_optional_and_off_by_default_so_the_bundle_is_unchanged():
    manifest = json.loads((ROOT / "packs/climate-environment/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    feature = next(f for f in manifest["optional_features"] if f["id"] == "biodiversity")
    assert feature["default"] is False
    assert not any(r["capability"].startswith("environment.biodiversity") for r in manifest["requires"])
    off = plan_for()
    assert "environment.biodiversity" not in bound(off) and PACK not in off["source_packs"]
    assert {"pack": "climate-environment", "feature": "biodiversity", "reason": "not selected"} in off["omissions"]
    on = plan_for(["biodiversity"])
    # The feature's source pack is pinned by its provider only; the bundle's own pins stay as they were.
    assert "environment.biodiversity" in bound(on) and on["source_packs"] == off["source_packs"]
    assert bound(off) <= bound(on) and "platform.entity-identity" in bound(on)
    view = CompositionView(on, provider_descriptors(), adapt_all().values())
    assert view.tools["noesis-knowledge-engine.occurrences_for_taxon_or_place"].provider == "environment.biodiversity"
    assert not list(ROOT.glob("packs/*biodiversity*"))  # no new pack


def test_feature_selection_follows_the_active_composition_and_readiness_reports_it():
    assert readiness(duckdb.connect(":memory:"), "environment")["selected"] is False
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("climate-environment", bundles["climate-environment"]["version"], features=["biodiversity"])
    assert coordinator.activate("climate-environment-biodiversity-on")["status"] == "published"
    assert feature_enabled(conn) is True
    coordinator.select("climate-environment", bundles["climate-environment"]["version"], features=[])
    coordinator.activate("climate-environment-biodiversity-off")
    assert feature_enabled(conn) is False
