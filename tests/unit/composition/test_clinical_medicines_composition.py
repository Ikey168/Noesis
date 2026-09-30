"""The Clinical Evidence bundle's optional ``medicines`` feature and the ``clinical.medicines`` provider (#2214)."""

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
from src.kb.clinical_medicines import BOUNDARY, feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.clinical import MEDICINES_SCOPES, MEDICINES_TOOLS, MEDICINES_WRITES

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "clinical-evidence", "version": "0.1.2", "range": "^0.1.2"}
CAPABILITIES = {"clinical.medicines-regulation", "clinical.medicines-identity", "clinical.medicines-monitoring"}


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
    """Today's Clinical Evidence manifest: the same bundle without the medicines contributions."""
    manifest = copy.deepcopy(bundles["clinical-evidence"])
    body = {k: v for k, v in manifest.items() if k != "content_hash"}
    body["optional_features"] = [f for f in body["optional_features"] if f["id"] != "medicines"]
    contributes = body["contributes"]
    contributes["capabilities"] = [c for c in contributes["capabilities"] if c["id"] not in CAPABILITIES]
    contributes["providers"] = [p for p in contributes["providers"] if p["id"] != "clinical.medicines"]
    contributes["profiles"] = [p for p in contributes["profiles"] if p["id"] != "clinical.medicines-timeline"]
    return {**bundles, "clinical-evidence": seal_manifest(body)}


def test_descriptor_declares_capabilities_operations_scopes_stores_probes_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "clinical.medicines")
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {"name": "src.kb.clinical_medicines", "version": "1.0.0",
                                            "server": "noesis-knowledge-engine"}
    assert {c["id"] for c in descriptor["capabilities"]} == CAPABILITIES
    tools = {o["tool"].split(".", 1)[1]: o for o in descriptor["operations"]}
    assert set(tools) == MEDICINES_TOOLS
    for name, operation in tools.items():
        assert operation["required_scopes"] == MEDICINES_SCOPES[name]
        assert operation["side_effect"] == ("local-mutation" if name in MEDICINES_WRITES else "read-only")
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "clinical.medicines"
              for s in d["stores"]}
    assert not owned & others
    # Medicines records live in the clinical record store (clinical.core); no new record or vintage store.
    assert {t for s in descriptor["stores"] for t in s["tables"]} == {"clinical_medicine_matches",
                                                                      "clinical_medicine_match_decisions"}
    assert descriptor["source_packs"] == [PACK]
    config = json.loads((ROOT / "config/source_packs/clinical-evidence.json").read_text())
    assert config["version"] == "0.1.4"  # 0.1.3 health capacity (#2215), 0.1.4 medical devices (#2654); ^0.1.2 holds
    assert {s["source_id"] for s in config["sources"]} >= {"medicines-ema-epar", "medicines-drugsfda-submissions",
                                                           "medicines-dailymed-spl", "medicines-fda-dsc"}


def test_the_feature_is_declared_off_by_default_with_its_capability_requirements():
    manifest = json.loads((ROOT / "packs/clinical-evidence/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    feature = next(f for f in manifest["optional_features"] if f["id"] == "medicines")
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == CAPABILITIES | {"clinical.terms", "clinical.trials"}
    profile = next(p for p in manifest["contributes"]["profiles"] if p["id"] == "clinical.medicines-timeline")
    assert profile["workflow_defaults"]["never"] == BOUNDARY
    assert not list(ROOT.glob("packs/*medicine*")) and not list(ROOT.glob("config/source_packs/*medicine*"))


def test_with_the_feature_off_the_bundle_is_unchanged():
    bundles = adapt_all()
    today = plan_for(bundles=without_feature(bundles),
                     descriptors=[d for d in provider_descriptors() if d["id"] != "clinical.medicines"])
    off = plan_for(bundles=bundles)
    assert json.dumps(off["bindings"], sort_keys=True) == json.dumps(today["bindings"], sort_keys=True)
    assert off["source_packs"] == today["source_packs"]
    assert "clinical.medicines" not in {b["provider"] for b in off["bindings"]}
    view = CompositionView(off, provider_descriptors(), adapt_all().values())
    assert not {f"noesis-knowledge-engine.{t}" for t in MEDICINES_TOOLS} & set(view.tools)


def test_with_the_feature_on_the_provider_binds_and_its_tools_are_exposed():
    plan = plan_for(["medicines"])
    assert {b["capability"] for b in plan["bindings"] if b["provider"] == "clinical.medicines"} == CAPABILITIES
    installed = json.loads((ROOT / "config/source_packs/clinical-evidence.json").read_text())["version"]
    (pin,) = [ref for ref in plan["source_packs"] if ref["pack_id"] == "clinical-evidence"]
    assert satisfies(installed, pin["range"]) and satisfies(installed, PACK["range"])
    stores = {}
    for descriptor in provider_descriptors():
        for store in descriptor["stores"]:
            stores.setdefault(store["record_type"], set()).add(descriptor["id"])
    assert all(len(owners) == 1 for owners in stores.values())
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert {f"noesis-knowledge-engine.{t}" for t in MEDICINES_TOOLS} <= set(view.tools)
    both = plan_for(["medicines", "surveillance"])
    assert {"clinical.medicines", "clinical.surveillance"} <= {b["provider"] for b in both["bindings"]}


def test_enabling_is_a_selection_change_with_a_receipt():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("clinical-evidence", bundles["clinical-evidence"]["version"], features=["medicines"])
    receipt = coordinator.activate("clinical-medicines-on")
    assert receipt["status"] == "published" and feature_enabled(conn) is True
    assert "clinical.medicines" in {b["provider"] for b in coordinator.active()["plan"]["bindings"]}
    result = clinical_bundle.set_enabled(conn, "clinical", False, principal_id="operator", scopes={"operator"})
    assert result["enabled"] is False and feature_enabled(conn) is False
    assert not [t for t in MEDICINES_TOOLS if t.startswith("set_") and t.endswith("_enabled")]
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `medicines` feature" in doc and "`noesis-clinical-medicines-record-v1`" in doc
