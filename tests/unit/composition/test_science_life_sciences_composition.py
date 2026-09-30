"""The Science bundle's optional life-sciences features and the ``science.life-sciences`` provider (#2652, LS12 #2711)."""

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
from src.kb.lifesci_records import FEATURES, feature_state
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.lifesci import LIFESCI_TOOLS, readiness

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "primary-scientific-evidence", "version": "1.2.0", "range": "^1.2.0"}
LIFE_SCIENCES = sorted(FEATURES.values())


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def science_plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "science", "version": bundles["science"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()),
                     descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "science" in b["consumers"]}


def test_descriptor_declares_operations_stores_probe_constraints_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "science.life-sciences")
    assert validate_provider_descriptor(descriptor) == []
    raw = json.loads((ROOT / "packs/science/providers/science.life-sciences.json").read_text())
    assert validate_provider_descriptor(raw) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-lifesci-record", "version": "1.0.0"}
    constraints = capability["semantic_constraints"]
    assert "no activity prediction" in constraints["exclusions"] and "no person names" in constraints["minimisation"]
    assert {o["tool"].split(".", 1)[1] for o in descriptor["operations"]} == LIFESCI_TOOLS
    assert descriptor["readiness_probes"] == [{"id": "lifesci-revisions", "kind": "table-exists",
                                               "target": "lifesci_revisions"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != descriptor["id"] for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("lifesci_") for s in descriptor["stores"] for t in s["tables"])
    assert not (ROOT / "packs/life-sciences").exists()  # no new pack: the Science bundle gains the provider


def test_each_source_is_a_separate_optional_feature_off_by_default():
    composition = json.loads((ROOT / "packs/science/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(LIFE_SCIENCES) <= set(features)
    for feature in LIFE_SCIENCES:
        assert features[feature]["default"] is False
        assert {r["capability"] for r in features[feature]["requires"]} == {
            "science.life-sciences", "platform.entity-identity", "platform.subscriptions",
            "platform.source-acquisition"}
    assert "science.life-sciences" in {p["id"] for p in composition["contributes"]["providers"]}
    assert validate_composition_manifest(adapt_all()["science"]) == []
    pack = json.loads((ROOT / "packs/science/pack.json").read_text())
    assert pack["schema_versions"]["lifesci-record"] == "1.0.0"
    assert "activity, binding or toxicity prediction" in pack["exclusions"]
    assert {e["tool"] for e in pack["query_examples"]} >= {"lifesci_entry_as_of", "lifesci_target_activities"}
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["science.life-sciences"] == {
        "subdomains": ["life-sciences-reference"], "shapes": ["registry-records", "observations"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `life-sciences-reference` |" not in program


@pytest.mark.parametrize("selection", [[], ["life-sciences-uniprot"], ["life-sciences-chembl"], LIFE_SCIENCES])
def test_each_selection_resolves_and_off_keeps_todays_bindings(selection):
    plan = science_plan(selection)
    assert sorted(plan["features"]["science"]) == sorted(selection)
    on = bool(selection)
    assert ("science.life-sciences" in bound(plan)) == on
    if on:
        assert {"platform.subscriptions", "platform.source-runtime"} <= bound(plan)
        assert PACK in plan["source_packs"]
        # Chemicals, Biodiversity and Clinical are never required by the life-sciences features.
        assert not {"chemicals.substances", "environment.biodiversity", "clinical.core"} & bound(plan)
    else:
        assert bound(plan) == bound(science_plan([]))
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.lifesci_entry_as_of" in view.tools) == on


def test_readiness_and_enablement_follow_the_active_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert {f["selected"] for f in report["features"].values()} == {False}
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    conn, coordinator, bundles, _ = _migrated()
    assert feature_state(conn) == dict.fromkeys(FEATURES, False)
    coordinator.select("science", bundles["science"]["version"], features=["life-sciences-pdb"])
    assert coordinator.activate("science-lifesci-pdb")["status"] == "published"
    assert feature_state(conn) == {"uniprot": False, "ncbi": False, "pdb": True, "chembl": False}
    coordinator.select("science", bundles["science"]["version"], features=[])
    coordinator.activate("science-lifesci-off")
    assert feature_state(conn) == dict.fromkeys(FEATURES, False)
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "`life-sciences-uniprot`" in doc and "`noesis-lifesci-record-v1`" in doc
