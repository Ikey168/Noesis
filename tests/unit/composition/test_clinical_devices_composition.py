"""The Clinical Evidence bundle's optional medical-devices features and the ``clinical.devices`` provider (#2714)."""

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
from src.kb.medical_devices_records import FEATURES, feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.medical_devices import (
    MEDICAL_DEVICES_SCOPES,
    MEDICAL_DEVICES_TOOLS,
    MEDICAL_DEVICES_WRITES,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "clinical-evidence", "version": "0.1.4", "range": "^0.1.4"}
PROVIDER = "clinical.devices"
CAPABILITIES = {"clinical.devices-records", "clinical.devices-identity", "clinical.devices-monitoring"}
PROFILE = "clinical.devices-profile"


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


def without_features(bundles):
    """The Clinical Evidence manifest without the medical-devices contributions."""
    manifest = copy.deepcopy(bundles["clinical-evidence"])
    body = {k: v for k, v in manifest.items() if k != "content_hash"}
    body["optional_features"] = [f for f in body["optional_features"] if f["id"] not in FEATURES]
    contributes = body["contributes"]
    contributes["capabilities"] = [c for c in contributes["capabilities"] if c["id"] not in CAPABILITIES]
    contributes["providers"] = [p for p in contributes["providers"] if p["id"] != PROVIDER]
    contributes["profiles"] = [p for p in contributes["profiles"] if p["id"] != PROFILE]
    return {**bundles, "clinical-evidence": seal_manifest(body)}


def test_descriptor_declares_capabilities_operations_scopes_stores_exclusions_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == PROVIDER)
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {"name": "src.kb.medical_devices_records", "version": "1.0.0",
                                            "server": "noesis-knowledge-engine"}
    assert {c["id"] for c in descriptor["capabilities"]} == CAPABILITIES
    records = next(c for c in descriptor["capabilities"] if c["id"] == "clinical.devices-records")
    constraints = records["semantic_constraints"]
    assert "no safety-signal detection" in constraints["exclusions"] and "MD01" in constraints["minimisation"]
    assert "never rates" in constraints["adverse_events"]
    tools = {o["tool"].split(".", 1)[1]: o for o in descriptor["operations"]}
    assert set(tools) == MEDICAL_DEVICES_TOOLS
    for name, operation in tools.items():
        assert operation["required_scopes"] == MEDICAL_DEVICES_SCOPES[name]
        assert operation["side_effect"] == ("local-mutation" if name in MEDICAL_DEVICES_WRITES else "read-only")
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != PROVIDER for s in d["stores"]}
    assert not owned & others  # no second identity, subscription or product-safety store
    assert descriptor["source_packs"] == [PACK]
    config = json.loads((ROOT / "config/source_packs/clinical-evidence.json").read_text())
    assert config["version"] == "0.1.4"
    for provider in ("clinical.core", "clinical.surveillance", "clinical.medicines", "clinical.health-capacity"):
        assert json.loads((ROOT / f"packs/clinical-evidence/providers/{provider}.json").read_text())["id"] == provider


def test_fda_gudid_and_eudamed_are_separate_optional_features_off_by_default():
    manifest = json.loads((ROOT / "packs/clinical-evidence/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    features = {f["id"]: f for f in manifest["optional_features"]}
    for feature in FEATURES:
        assert features[feature]["default"] is False
        required = {r["capability"] for r in features[feature]["requires"]}
        assert CAPABILITIES <= required
        # Product safety, Medicines, trials and ownership links degrade when absent, so they are never required
        assert not required & {"products.safety-notices", "clinical.medicines-regulation", "ownership.graph",
                               "products.identities"}
    profile = next(p for p in manifest["contributes"]["profiles"] if p["id"] == PROFILE)
    assert "no safety-signal detection" in profile["workflow_defaults"]["never"]
    assert not list(ROOT.glob("packs/*device*")) and not list(ROOT.glob("config/source_packs/*device*"))


def test_with_the_features_off_the_bundle_is_unchanged():
    bundles = adapt_all()
    today = plan_for(bundles=without_features(bundles),
                     descriptors=[d for d in provider_descriptors() if d["id"] != PROVIDER])
    off = plan_for(bundles=bundles)
    assert json.dumps(off["bindings"], sort_keys=True) == json.dumps(today["bindings"], sort_keys=True)
    assert off["source_packs"] == today["source_packs"]
    view = CompositionView(off, provider_descriptors(), adapt_all().values())
    assert not {f"noesis-knowledge-engine.{t}" for t in MEDICAL_DEVICES_TOOLS} & set(view.tools)


@pytest.mark.parametrize("selection", [["medical-devices-fda"], ["medical-devices-gudid"],
                                       ["medical-devices-eudamed"], list(FEATURES)])
def test_each_feature_binds_the_provider_and_its_tools(selection):
    plan = plan_for(selection)
    bound = {b["capability"]: b["provider"] for b in plan["bindings"]}
    assert {c for c, p in bound.items() if p == PROVIDER} == CAPABILITIES
    assert bound["ownership.identity"] == "ownership.core"
    installed = json.loads((ROOT / "config/source_packs/clinical-evidence.json").read_text())["version"]
    (pin,) = [ref for ref in plan["source_packs"] if ref["pack_id"] == "clinical-evidence"]
    assert satisfies(installed, pin["range"]) and satisfies(installed, PACK["range"])
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert {f"noesis-knowledge-engine.{t}" for t in MEDICAL_DEVICES_TOOLS} <= set(view.tools)


def test_features_coexist_with_the_other_clinical_features():
    plan = plan_for([*FEATURES, "medicines", "surveillance", "health-capacity"])
    assert {PROVIDER, "clinical.medicines", "clinical.surveillance", "clinical.health-capacity"} <= {
        b["provider"] for b in plan["bindings"]}


def test_enabling_is_a_selection_change_with_a_receipt():
    conn, coordinator, bundles, _ = _migrated()
    assert not any(feature_enabled(conn, f) for f in FEATURES)
    coordinator.select("clinical-evidence", bundles["clinical-evidence"]["version"], features=["medical-devices-fda"])
    receipt = coordinator.activate("clinical-devices-fda-on")
    assert receipt["status"] == "published"
    assert feature_enabled(conn, "medical-devices-fda") and not feature_enabled(conn, "medical-devices-eudamed")
    result = clinical_bundle.set_enabled(conn, "clinical", False, principal_id="operator", scopes={"operator"})
    assert result["enabled"] is False and not feature_enabled(conn, "medical-devices-fda")
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `medical-devices-fda`" in doc
