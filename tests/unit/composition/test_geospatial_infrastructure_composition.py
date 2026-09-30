"""The Geospatial bundle's optional ``infrastructure`` features and the ``geospatial.infrastructure`` provider (#2393)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import satisfies, validate_composition_manifest, validate_provider_descriptor
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.infrastructure_assets import feature_enabled
from src.kb.infrastructure_queries import readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURES = ("infrastructure", "infrastructure-ownership", "infrastructure-citation-links")


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def plan_for(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "geospatial", "version": bundles["geospatial"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "geospatial" in b["consumers"]}


def test_descriptor_declares_capabilities_scoped_operations_stores_probes_and_the_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "geospatial.infrastructure")
    assert validate_provider_descriptor(descriptor) == []
    assert {c["id"] for c in descriptor["capabilities"]} == {"geospatial.infrastructure-assets",
                                                            "geospatial.infrastructure-answers"}
    manifest = json.loads((ROOT / "config/source_packs/geospatial-infrastructure.json").read_text())
    assert satisfies(manifest["version"], descriptor["source_packs"][0]["range"])
    assert manifest["domains"] == ["geospatial"]
    from tools.knowledge_engine_mcp.infrastructure import INFRASTRUCTURE_SCOPES

    for operation in descriptor["operations"]:
        tool = operation["tool"].split(".", 1)[1]
        assert operation["required_scopes"] == INFRASTRUCTURE_SCOPES[tool]
    owned = {t for s in descriptor["stores"] for t in s["tables"]}
    others = {t for d in provider_descriptors() if d["id"] != "geospatial.infrastructure" for s in d["stores"]
              for t in s["tables"]}
    assert not owned & others and all(t.startswith("infra_") for t in owned)
    assert {p["target"] for p in descriptor["readiness_probes"]} == {"infra_asset_revisions", "infra_asset_matches",
                                                                   "infra_links", "infra_monitors"}


def test_features_are_off_by_default_and_the_bundle_pin_is_unchanged():
    composition = json.loads((ROOT / "packs/geospatial/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert all(features[f]["default"] is False for f in FEATURES)
    assert composition["contributes"]["source_packs"] == [
        {"pack_id": "geospatial-berlin", "version": "1.1.0", "range": "^1.1.0"}]
    assert validate_composition_manifest(adapt_all()["geospatial"]) == []
    assert not list(ROOT.glob("packs/*infrastructure*"))  # no new pack


@pytest.mark.parametrize("selection", [[], ["infrastructure"], list(FEATURES)])
def test_each_selection_binds_only_what_it_needs(selection):
    plan = plan_for(selection)
    providers = bound(plan)
    assert ("geospatial.infrastructure" in providers) == bool(selection)
    if selection == ["infrastructure"]:
        assert not {"ownership.core", "energy.core", "legal.core"} & providers
    if selection == list(FEATURES):
        assert {"ownership.core", "legal.core"} <= providers
        # Energy Systems and Climate and Environment depend on Geospatial: declaring them would be a cycle, so
        # their links are read-only at run time when installed and skipped otherwise (readiness reports which).
        assert not {"energy.core", "environment.core"} & providers
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.infrastructure_assets_in_place" in view.tools) == bool(selection)


@pytest.mark.parametrize(("missing", "feature"), [("ownership.core", "infrastructure-ownership"),
                                                  ("legal.core", "infrastructure-citation-links")])
def test_absent_ownership_or_energy_degrades_to_a_visible_omission(missing, feature):
    descriptors = [d for d in provider_descriptors() if d["id"] != missing]
    plan = plan_for(["infrastructure", feature], descriptors)
    assert "geospatial.infrastructure" in bound(plan)  # the core feature keeps working
    assert any(o["feature"] == feature for o in plan["omissions"])


def test_readiness_reports_live_verification_per_source_and_optional_packs():
    conn = duckdb.connect(":memory:")
    report = readiness(conn, "infrastructure", scopes={"operator"})
    assert report["selected"] is False and report["stores_ready"] is False
    assert all(p["live_verification"]["status"] == "unverified-live" for p in report["providers"].values())
    assert report["optional_packs"]["corporate-ownership"]["installed"] is False
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `infrastructure` feature" in doc and "`noesis-infrastructure-asset-record-v1`" in doc


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("geospatial", bundles["geospatial"]["version"], features=["infrastructure"])
    assert coordinator.activate("geospatial-infrastructure-on")["status"] == "published"
    assert feature_enabled(conn) is True
