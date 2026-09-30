"""The Clinical Evidence bundle's optional ``health-capacity`` feature and the ``clinical.health-capacity`` provider.

#2215 (HS10). The issue names the feature ``health_capacity``; composition feature ids are kebab-case, so the
manifest declares ``health-capacity``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    satisfies,
    seal_manifest,
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb import clinical_bundle
from src.kb.health_capacity import FEATURE, NEVER_SENTENCE, feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.clinical import (
    HEALTH_CAPACITY_SCOPES,
    HEALTH_CAPACITY_TOOLS,
    HEALTH_CAPACITY_WRITES,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "clinical-evidence", "version": "0.1.3", "range": "^0.1.3"}
PROVIDER = "clinical.health-capacity"
CAPABILITIES = {"clinical.health-capacity-indicators", "clinical.health-capacity-alignment",
                "clinical.health-capacity-monitoring"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def plan_for(features=None, bundles=None, descriptors=None):
    bundles = bundles or adapt_all()
    roots = [{"pack": "clinical-evidence", "version": bundles["clinical-evidence"]["version"]},
             {"pack": "science", "version": bundles["science"]["version"]}]
    if features is not None:
        roots[0]["features"] = features
    result = resolve(roots, list(bundles.values()),
                     descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def without_feature(bundles):
    """Today's Clinical Evidence manifest: the same bundle without the health-capacity contributions."""
    manifest = copy.deepcopy(bundles["clinical-evidence"])
    body = {k: v for k, v in manifest.items() if k != "content_hash"}
    body["optional_features"] = [f for f in body["optional_features"] if f["id"] != FEATURE]
    contributes = body["contributes"]
    contributes["capabilities"] = [c for c in contributes["capabilities"] if c["id"] not in CAPABILITIES]
    contributes["providers"] = [p for p in contributes["providers"] if p["id"] != PROVIDER]
    contributes["profiles"] = [p for p in contributes["profiles"] if p["id"] != "clinical.health-capacity-profile"]
    return {**bundles, "clinical-evidence": seal_manifest(body)}


def test_descriptor_declares_capabilities_operations_scopes_stores_probes_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == PROVIDER)
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {"name": "src.kb.health_capacity", "version": "1.0.0",
                                            "server": "noesis-knowledge-engine"}
    assert {c["id"] for c in descriptor["capabilities"]} == CAPABILITIES
    tools = {o["tool"].split(".", 1)[1]: o for o in descriptor["operations"]}
    assert set(tools) == HEALTH_CAPACITY_TOOLS
    for name, operation in tools.items():
        assert operation["required_scopes"] == HEALTH_CAPACITY_SCOPES[name]
        assert operation["side_effect"] == ("local-mutation" if name in HEALTH_CAPACITY_WRITES else "read-only")
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != PROVIDER for s in d["stores"]}
    assert not owned & others
    # Series, definitions and vintages stay in the surveillance storage: no new series or vintage table.
    tables = {t for s in descriptor["stores"] for t in s["tables"]}
    assert tables == {"health_capacity_place_resolutions", "health_capacity_mappings", "health_capacity_notes",
                      "health_capacity_economic_links"}
    assert descriptor["source_packs"] == [PACK]
    config = json.loads((ROOT / "config/source_packs/clinical-evidence.json").read_text())
    assert config["version"] == "0.1.3"
    assert {s["source_id"] for s in config["sources"]} >= {"who-gho-health-capacity", "oecd-health-statistics",
                                                           "eurostat-health-care-resources"}


def test_the_feature_is_declared_off_by_default_with_its_capability_requirements():
    manifest = json.loads((ROOT / "packs/clinical-evidence/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    feature = next(f for f in manifest["optional_features"] if f["id"] == FEATURE)
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == CAPABILITIES | {
        "geospatial.place-resolution", "clinical.surveillance-series", "clinical.surveillance-vintages"}
    profile = next(p for p in manifest["contributes"]["profiles"] if p["id"] == "clinical.health-capacity-profile")
    assert profile["workflow_defaults"]["never"] == NEVER_SENTENCE
    assert not list(ROOT.glob("packs/*capacity*")) and not list(ROOT.glob("config/source_packs/*capacity*"))


def test_with_the_feature_off_the_bundle_is_unchanged():
    bundles = adapt_all()
    today = plan_for(bundles=without_feature(bundles),
                     descriptors=[d for d in provider_descriptors() if d["id"] != PROVIDER])
    off = plan_for(bundles=bundles)
    assert json.dumps(off["bindings"], sort_keys=True) == json.dumps(today["bindings"], sort_keys=True)
    assert off["source_packs"] == today["source_packs"]
    assert PROVIDER not in {b["provider"] for b in off["bindings"]}
    view = CompositionView(off, provider_descriptors(), adapt_all().values())
    assert not {f"noesis-knowledge-engine.{t}" for t in HEALTH_CAPACITY_TOOLS} & set(view.tools)


def test_with_the_feature_on_the_provider_binds_and_its_tools_are_exposed():
    plan = plan_for([FEATURE])
    bound = {b["capability"]: b["provider"] for b in plan["bindings"]}
    assert {c for c, p in bound.items() if p == PROVIDER} == CAPABILITIES
    assert bound["clinical.surveillance-series"] == "clinical.surveillance"
    assert bound["geospatial.place-resolution"] == "geospatial.core"
    installed = json.loads((ROOT / "config/source_packs/clinical-evidence.json").read_text())["version"]
    (pin,) = [ref for ref in plan["source_packs"] if ref["pack_id"] == "clinical-evidence"]
    assert satisfies(installed, pin["range"]) and satisfies(installed, PACK["range"])
    stores = {}
    for descriptor in provider_descriptors():
        for store in descriptor["stores"]:
            stores.setdefault(store["record_type"], set()).add(descriptor["id"])
    assert all(len(owners) == 1 for owners in stores.values())
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert {f"noesis-knowledge-engine.{t}" for t in HEALTH_CAPACITY_TOOLS} <= set(view.tools)
    every = plan_for([FEATURE, "medicines", "surveillance"])
    assert {PROVIDER, "clinical.medicines", "clinical.surveillance"} <= {b["provider"] for b in every["bindings"]}


def test_enabling_is_a_selection_change_with_a_receipt():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("clinical-evidence", bundles["clinical-evidence"]["version"], features=[FEATURE])
    receipt = coordinator.activate("clinical-health-capacity-on")
    assert receipt["status"] == "published" and feature_enabled(conn) is True
    assert PROVIDER in {b["provider"] for b in coordinator.active()["plan"]["bindings"]}
    result = clinical_bundle.set_enabled(conn, "clinical", False, principal_id="operator", scopes={"operator"})
    assert result["enabled"] is False and feature_enabled(conn) is False
    assert not [t for t in HEALTH_CAPACITY_TOOLS if t.startswith("set_") and t.endswith("_enabled")]
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `health-capacity` feature" in doc
