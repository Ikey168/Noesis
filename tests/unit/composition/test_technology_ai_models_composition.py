"""The Technology bundle's optional AI models features and the ``technology.ai-models`` provider (AI11, #2799; #2742)."""

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
from src.ingestion.source_packs import validate_source_pack
from src.kb.ai_models_records import features_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
BASE = {"technology.core", "technology.patents", "technology.standards"}
FEATURES = ("ai-models-hub", "ai-models-openml", "ai-models-epoch")
FEATURE_PROVIDERS = BASE | {
    "technology.ai-models",
    "platform.entity-identity",
    "platform.subscriptions",
    "platform.source-runtime",
}
PACK = {"pack_id": "technology-ai-models", "version": "1.0.0", "range": "^1.0.0"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def technology_plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "technology", "version": bundles["technology"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()),
                     descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "technology" in b["consumers"]}


def test_descriptor_declares_capability_read_only_operations_stores_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "technology.ai-models")
    assert validate_provider_descriptor(descriptor) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-ai-model-record", "version": "2.0.0"}
    constraints = capability["semantic_constraints"]
    assert {"revisions", "cards", "observations", "licences", "as_of", "identity", "links", "minimisation",
            "exclusions"} <= set(constraints)
    assert "never stored" in constraints["cards"] and "never merged" in constraints["observations"]
    assert "a shared name is never a match" in constraints["identity"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    tools = {o["tool"].split(".", 1)[1] for o in descriptor["operations"]}
    assert {"ai_model_records_as_of", "ai_model_revision_history", "export_ai_model_evidence"} <= tools
    assert descriptor["readiness_probes"] == [{"id": "revisions", "kind": "table-exists", "target": "ai_revisions"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "technology.ai-models"
              for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("ai_") for s in descriptor["stores"] for t in s["tables"])


def test_the_ai_models_features_are_optional_default_off_per_source_in_the_existing_technology_pack():
    composition = json.loads((ROOT / "packs/technology/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert composition["optional_features"][0]["id"] == "vulnerabilities"  # existing feature unchanged and first
    for feature_id in FEATURES:
        feature = features[feature_id]
        assert feature["default"] is False
        assert {r["capability"] for r in feature["requires"]} == {
            "technology.ai-models", "platform.entity-identity", "platform.subscriptions",
            "platform.source-acquisition"}
        # OSS ecosystems and Literature are not required: their links degrade to provider_absent.
        assert "provider_absent" in feature["description"] and "no licence-compliance" in feature["description"]
    profile = next(p for p in composition["contributes"]["profiles"] if p["id"] == "technology.ai-models")
    assert {"revision", "sha", "vintage", "self-reported result", "not-returned", "withdrawn"} <= set(
        profile["vocabulary"])
    assert PACK in composition["contributes"]["source_packs"]
    assert {"pack_id": "technical-software-knowledge", "version": "1.2.0", "range": "^1.2.0"} in composition[
        "contributes"]["source_packs"]
    installed = validate_source_pack(json.loads((ROOT / "config/source_packs/technology-ai-models.json").read_text()))
    assert installed["version"] == PACK["version"]
    assert validate_composition_manifest(adapt_all()["technology"]) == []
    pack = json.loads((ROOT / "packs/technology/pack.json").read_text())
    assert {"model weights or dataset file downloads",
            "licence-compliance interpretation of declared model or dataset licences",
            "model or dataset leaderboards, rankings and download, like or trending counts",
            "merged self-reported results, OpenML evaluations and Epoch estimates"} <= set(pack["exclusions"])
    assert pack["schema_versions"]["ai-model-record"] == "2.0.0"
    assert {"ai-model-dataset-registry-revisions", "ai-model-cross-source-identity"} <= set(pack["capabilities"])
    assert {"model_card", "self_reported_result", "epoch_model_entry"} <= set(
        pack["ontology_extensions"]["object_types"])
    assert "model card" in pack["planner_keywords"]["library"]
    assert not list(ROOT.glob("packs/*ai-model*")) and not list(ROOT.glob("packs/*ai_model*"))  # no new pack


@pytest.mark.parametrize("selection", [[], ["ai-models-hub"], ["ai-models-openml"], ["ai-models-epoch"],
                                       list(FEATURES), ["ai-models-hub", "vulnerabilities"]])
def test_each_selection_resolves_independently_and_together(selection):
    plan = technology_plan(selection)
    selected = any(f in selection for f in FEATURES)
    assert ("technology.ai-models" in bound(plan)) == selected
    if selection == []:
        assert bound(plan) == BASE
        assert {"pack": "technology", "feature": "ai-models-hub", "reason": "not selected"} in plan["omissions"]
    if selection in (["ai-models-hub"], ["ai-models-openml"], ["ai-models-epoch"], list(FEATURES)):
        assert bound(plan) == FEATURE_PROVIDERS
        assert PACK in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.ai_model_records_as_of" in view.tools) == selected
    assert "noesis-kb.kb_technical" in view.tools


def test_oss_and_literature_are_not_bound_and_links_degrade_to_provider_absent():
    plan = technology_plan(["ai-models-hub"])
    assert not {"oss.registries", "science.literature", "science.research-entities"} & bound(plan)
    descriptors = [d for d in provider_descriptors() if d["id"] != "platform.entity-identity"]
    plan = technology_plan(["ai-models-epoch"], descriptors)
    omission = next(o for o in plan["omissions"] if o["feature"] == "ai-models-epoch")
    assert omission["capability"] == "platform.entity-identity" and "missing_contract" in omission["reason"]
    from src.kb.ai_models_links import AiModelsLinks
    from tests.unit import ai_models_harness as h

    conn = h.connection()
    h.apply(conn, "hub", retrieved_at_ms=h.FIRST_RETRIEVAL)
    AiModelsLinks(conn).link(h.NS, principal_id="svc", scopes=h.SCOPES)
    states = {x["kind"]: x["state"] for x in AiModelsLinks(conn).links(h.NS, scopes=h.READ_ONLY)}
    assert states["literature"] == states["dataset-doi"] == states["oss-package"] == "provider_absent"


def test_readiness_and_feature_enablement_follow_the_composition_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["features"] == dict.fromkeys(FEATURES, False) and report["stores_ready"] is False
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    conn, coordinator, bundles, _ = _migrated()
    assert features_enabled(conn) == dict.fromkeys(FEATURES, False)
    coordinator.select("technology", bundles["technology"]["version"], features=["ai-models-hub", "ai-models-epoch"])
    assert coordinator.activate("technology-ai-models-on")["status"] == "published"
    assert features_enabled(conn) == {"ai-models-hub": True, "ai-models-openml": False, "ai-models-epoch": True}
    coordinator.select("technology", bundles["technology"]["version"], features=[])
    coordinator.activate("technology-ai-models-off")
    assert features_enabled(conn) == dict.fromkeys(FEATURES, False)
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "`ai-models-hub`, `ai-models-openml` and `ai-models-epoch`" in doc
    assert "`noesis-ai-model-record-v2`" in doc


def test_taxonomy_classifies_the_provider_offline_and_the_gap_row_is_removed():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["technology.ai-models"] == {
        "subdomains": ["ai-models-datasets"], "shapes": ["registry-records", "versioned-documents", "observations"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `ai-models-datasets` |" not in program
    assert "covered offline (not live) by `technology.ai-models`" in " ".join(program.split())
