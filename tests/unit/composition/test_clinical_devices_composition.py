"""The ``clinical.devices`` provider and its optional openFDA, GUDID and EUDAMED features (#2654, MD12)."""

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
from src.ingestion.medical_devices_sources import FEATURES, REVIEW_BOUNDARY
from src.kb import clinical_bundle
from src.kb.medical_devices_records import feature_enabled, selected_features
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.medical_devices import (
    DEVICE_SCOPES,
    DEVICE_TOOLS,
    DEVICE_WRITES,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "clinical-evidence", "version": "0.1.4", "range": "^0.1.4"}
PROVIDER = "clinical.devices"
CAPABILITIES = {"clinical.devices-records", "clinical.devices-identity", "clinical.devices-monitoring"}
FEATURE_IDS = set(FEATURES.values())


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


def without_provider(bundles):
    """The Clinical Evidence manifest without the medical-devices contributions (what it was before #2654)."""
    manifest = copy.deepcopy(bundles["clinical-evidence"])
    body = {k: v for k, v in manifest.items() if k != "content_hash"}
    body["optional_features"] = [f for f in body["optional_features"] if f["id"] not in FEATURE_IDS]
    contributes = body["contributes"]
    contributes["capabilities"] = [c for c in contributes["capabilities"] if c["id"] not in CAPABILITIES]
    contributes["providers"] = [p for p in contributes["providers"] if p["id"] != PROVIDER]
    contributes["profiles"] = [p for p in contributes["profiles"] if p["id"] != "clinical.devices-regulatory-history"]
    return {**bundles, "clinical-evidence": seal_manifest(body)}


def test_descriptor_declares_capabilities_operations_scopes_stores_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == PROVIDER)
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"]["name"] == "src.kb.medical_devices_records"
    assert {c["id"] for c in descriptor["capabilities"]} == CAPABILITIES
    tools = {o["tool"].split(".", 1)[1]: o for o in descriptor["operations"]}
    assert set(tools) == DEVICE_TOOLS
    for name, operation in tools.items():
        assert operation["required_scopes"] == DEVICE_SCOPES[name]
        assert operation["side_effect"] == ("local-mutation" if name in DEVICE_WRITES else "read-only")
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != PROVIDER for s in d["stores"]}
    assert not owned & others
    assert descriptor["source_packs"] == [PACK]
    constraints = json.dumps([c["semantic_constraints"] for c in descriptor["capabilities"]])
    for needle in ("no safety-signal detection", "never rates, incidence", "minimisation", "nothing is merged"):
        assert needle in constraints


def test_no_new_pack_and_three_features_off_by_default():
    manifest = json.loads((ROOT / "packs/clinical-evidence/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    assert PROVIDER in {p["id"] for p in manifest["contributes"]["providers"]}
    features = {f["id"]: f for f in manifest["optional_features"] if f["id"] in FEATURE_IDS}
    assert set(features) == FEATURE_IDS
    for feature in features.values():
        assert feature["default"] is False
        # Product safety, Medicines and ownership links degrade when absent: the features do not require them.
        assert {r["capability"] for r in feature["requires"]} == CAPABILITIES
    profile = next(p for p in manifest["contributes"]["profiles"] if p["id"] == "clinical.devices-regulatory-history")
    assert profile["workflow_defaults"]["never"] == REVIEW_BOUNDARY
    assert not list(ROOT.glob("packs/*device*")) and not list(ROOT.glob("config/source_packs/*device*"))
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"][PROVIDER] == {"subdomains": ["medical-devices"],
                                              "shapes": ["registry-records", "events-notices"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `medical-devices` |" not in program


def test_with_the_features_off_the_bundle_is_unchanged():
    bundles = adapt_all()
    today = plan_for(bundles=without_provider(bundles),
                     descriptors=[d for d in provider_descriptors() if d["id"] != PROVIDER])
    off = plan_for(bundles=bundles)
    assert json.dumps(off["bindings"], sort_keys=True) == json.dumps(today["bindings"], sort_keys=True)
    assert off["source_packs"] == today["source_packs"]
    view = CompositionView(off, provider_descriptors(), adapt_all().values())
    assert not {f"noesis-knowledge-engine.{t}" for t in DEVICE_TOOLS} & set(view.tools)


@pytest.mark.parametrize("feature", sorted(FEATURE_IDS))
def test_each_feature_on_its_own_binds_the_provider_and_exposes_its_tools(feature):
    plan = plan_for([feature])
    bound = {b["capability"]: b["provider"] for b in plan["bindings"]}
    assert {c for c, p in bound.items() if p == PROVIDER} == CAPABILITIES
    installed = json.loads((ROOT / "config/source_packs/clinical-evidence.json").read_text())["version"]
    (pin,) = [ref for ref in plan["source_packs"] if ref["pack_id"] == "clinical-evidence"]
    assert satisfies(installed, pin["range"]) and satisfies(installed, PACK["range"])
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert {f"noesis-knowledge-engine.{t}" for t in DEVICE_TOOLS} <= set(view.tools)
    every = plan_for(sorted(FEATURE_IDS) + ["medicines", "surveillance", "health-capacity"])
    assert {PROVIDER, "clinical.medicines", "clinical.surveillance"} <= {b["provider"] for b in every["bindings"]}


def test_selecting_a_feature_is_a_coordinator_selection_change():
    conn, coordinator, bundles, _ = _migrated()
    assert selected_features(conn) == []
    coordinator.select("clinical-evidence", bundles["clinical-evidence"]["version"],
                       features=["medical-devices-fda", "medical-devices-eudamed"])
    receipt = coordinator.activate("clinical-devices-on")
    assert receipt["status"] == "published"
    assert feature_enabled(conn, "medical-devices-fda") and not feature_enabled(conn, "medical-devices-gudid")
    result = clinical_bundle.set_enabled(conn, "clinical", False, principal_id="operator", scopes={"operator"})
    assert result["enabled"] is False and selected_features(conn) == []
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `medical-devices-fda`" in doc
