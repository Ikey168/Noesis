"""The Science bundle's optional life-sciences features and the ``science.life-sciences`` provider (#2652, LS12
#2711)."""

from __future__ import annotations

import json
import re
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
from src.kb.lifesci_store import readiness, selected_features
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "primary-scientific-evidence", "version": "1.2.0", "range": "^1.2.0"}
FEATURES = ("life-sciences-uniprot", "life-sciences-ncbi", "life-sciences-pdb", "life-sciences-chembl")


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
    result = resolve([root], list(bundles.values()), descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "science" in b["consumers"]}


def test_descriptor_declares_read_only_lookups_stores_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "science.life-sciences")
    assert validate_provider_descriptor(descriptor) == []
    raw = json.loads((ROOT / "packs/science/providers/science.life-sciences.json").read_text())
    assert validate_provider_descriptor(raw) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-lifesci-record", "version": "2.0.0"}
    constraints = capability["semantic_constraints"]
    assert "no biological or clinical inference" in constraints["exclusions"]
    assert "no personal names" in constraints["minimisation"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    assert {"entry-as-of", "compounds-for-target", "evidence-bundle"} <= {o["id"] for o in descriptor["operations"]}
    assert all("knowledge:lifesci:read" in o["required_scopes"] for o in descriptor["operations"])
    assert descriptor["readiness_probes"] == [{"id": "revisions", "kind": "table-exists",
                                               "target": "lifesci_revisions"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != descriptor["id"] for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("lifesci_") for s in descriptor["stores"] for t in s["tables"])


def test_each_source_is_a_separate_optional_feature_and_no_pack_is_created():
    composition = json.loads((ROOT / "packs/science/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    for feature in FEATURES:
        assert features[feature]["default"] is False
        assert {r["capability"] for r in features[feature]["requires"]} == {
            "science.life-sciences", "platform.entity-identity", "platform.subscriptions",
            "platform.source-acquisition"}
        assert "provider_absent" in features[feature]["description"]
    assert validate_composition_manifest(adapt_all()["science"]) == []
    assert not (ROOT / "packs/life-sciences").exists() and not (ROOT / "packs/lifesci").exists()
    manifest = json.loads((ROOT / "config/source_packs/scientific.json").read_text())
    assert {s["source_id"] for s in manifest["sources"] if s.get("connector") == "life-sciences"} == {
        "uniprot-lifesci-proteins", "ncbi-gene-lifesci", "ncbi-taxonomy-lifesci", "rcsb-pdb-lifesci-structures",
        "chembl-lifesci-bioactivity"}
    pack = json.loads((ROOT / "packs/science/pack.json").read_text())
    assert pack["schema_versions"]["lifesci-record"] == "2.0.0"
    assert "activity prediction, scoring or ranking of compounds" in pack["exclusions"]


@pytest.mark.parametrize("selection", [[], ["life-sciences-uniprot"], ["life-sciences-chembl"], list(FEATURES)])
def test_each_selection_resolves_and_off_keeps_todays_bindings(selection):
    plan = science_plan(selection)
    assert sorted(plan["features"]["science"]) == sorted(selection)
    on = bool(selection)
    assert ("science.life-sciences" in bound(plan)) == on
    if on:
        assert {"platform.subscriptions", "platform.source-runtime"} <= bound(plan)
        assert PACK in plan["source_packs"]
        # Chemicals, Clinical and Biodiversity are never required: links degrade gracefully.
        assert not {"chemicals.substances", "clinical.medicines", "environment.biodiversity"} & bound(plan)
    else:
        assert bound(plan) == bound(science_plan([]))
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.lifesci_entry_as_of" in view.tools) == on


def test_readiness_and_enablement_follow_the_active_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["stores_ready"] is False and not any(report["features"].values())
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    conn, coordinator, bundles, _ = _migrated()
    assert not set(FEATURES) & set(selected_features(conn))
    coordinator.select("science", bundles["science"]["version"], features=["life-sciences-uniprot",
                                                                            "life-sciences-chembl"])
    assert coordinator.activate("science-lifesci-on")["status"] == "published"
    assert readiness(conn)["features"] == {"life-sciences-chembl": True, "life-sciences-ncbi": False,
                                           "life-sciences-pdb": False, "life-sciences-uniprot": True}
    coordinator.select("science", bundles["science"]["version"], features=[])
    coordinator.activate("science-lifesci-off")
    assert not set(FEATURES) & set(selected_features(conn))
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "`life-sciences-uniprot`" in doc and "`noesis-lifesci-record-v2`" in doc


def test_the_taxonomy_classifies_the_provider_and_the_gap_row_is_gone():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["science.life-sciences"] == {"subdomains": ["life-sciences-reference"],
                                                              "shapes": ["registry-records", "observations"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "life-sciences-reference" not in re.findall(r"^\| `([a-z0-9-]+)` \|", program, re.MULTILINE)
