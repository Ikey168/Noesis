"""The Science bundle's optional ``research-entities-*`` features and the ``science.research-entities`` provider
(#2639), including the taxonomy gap closure."""

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
from src.kb.research_entities_records import feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "research-discovery", "version": "1.5.0", "range": "^1.5.0"}
FEATURES = ("research-entities-ror", "research-entities-orcid", "research-entities-datacite",
            "research-entities-cordis")


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def science_plan(features=None):
    bundles = adapt_all()
    root = {"pack": "science", "version": bundles["science"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "science" in b["consumers"]}


def test_descriptor_declares_read_only_operations_minimisation_stores_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "science.research-entities")
    assert validate_provider_descriptor(descriptor) == []
    raw = json.loads((ROOT / "packs/science/providers/science.research-entities.json").read_text())
    assert validate_provider_descriptor(raw) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-research-entity-record", "version": "2.0.0"}
    constraints = capability["semantic_constraints"]
    assert "no researcher rankings or metrics" in constraints["exclusions"]
    assert "researchers:read" in constraints["minimisation"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    assert {"researcher-as-of", "organisation-lineage", "datasets-for-paper", "evidence-bundle"} <= {
        o["id"] for o in descriptor["operations"]}
    researcher = next(o for o in descriptor["operations"] if o["id"] == "researcher-as-of")
    assert "knowledge:science:research-entities:researchers:read" in researcher["required_scopes"]
    assert descriptor["readiness_probes"] == [{"id": "revisions", "kind": "table-exists",
                                               "target": "research_entity_revisions"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != descriptor["id"] for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("research_entity_") for s in descriptor["stores"] for t in s["tables"])


def test_the_four_registries_are_separate_optional_features_off_by_default():
    composition = json.loads((ROOT / "packs/science/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    for feature_id in FEATURES:
        assert features[feature_id]["default"] is False
        assert {r["capability"] for r in features[feature_id]["requires"]} == {
            "science.research-entities", "platform.entity-identity", "platform.subscriptions",
            "platform.source-acquisition"}  # literature, funding and ownership links are never required
    assert validate_composition_manifest(adapt_all()["science"]) == []
    assert not (ROOT / "packs/research-entities").exists()  # no new pack; the Science bundle gains the provider
    pack = json.loads((ROOT / "packs/science/pack.json").read_text())
    assert "author disambiguation by name" in pack["exclusions"]
    manifest = json.loads((ROOT / "config/source_packs/research.json").read_text())
    assert {s["source_id"] for s in manifest["sources"] if s.get("connector") == "research-entities"} == {
        "research-entities-ror", "research-entities-orcid", "research-entities-datacite", "research-entities-cordis"}


@pytest.mark.parametrize("selection", [[], ["research-entities-ror"], ["research-entities-orcid"],
                                       list(FEATURES), ["education-statistics", "research-entities-cordis"]])
def test_each_selection_resolves_and_off_keeps_todays_bindings(selection):
    plan = science_plan(selection)
    assert sorted(plan["features"]["science"]) == sorted(selection)
    on = any(f in FEATURES for f in selection)
    assert ("science.research-entities" in bound(plan)) == on
    if on:
        assert {"platform.subscriptions", "platform.source-runtime"} <= bound(plan)
        assert PACK in plan["source_packs"]
    elif not selection:
        assert bound(plan) == bound(science_plan([]))
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.organisation_lineage_projects_datasets" in view.tools) == on


def test_readiness_and_enablement_follow_the_active_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["store_ready"] is False
    assert {f["selected"] for f in report["features"].values()} == {False}
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn, "research-entities-orcid") is False
    coordinator.select("science", bundles["science"]["version"], features=["research-entities-orcid"])
    assert coordinator.activate("science-research-entities-on")["status"] == "published"
    assert feature_enabled(conn, "research-entities-orcid") is True
    assert feature_enabled(conn, "research-entities-ror") is False
    coordinator.select("science", bundles["science"]["version"], features=[])
    coordinator.activate("science-research-entities-off")
    assert feature_enabled(conn, "research-entities-orcid") is False
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "`research-entities-orcid`" in doc and "`noesis-research-entity-record-v2`" in doc


def test_the_taxonomy_classifies_the_provider_and_the_gap_row_is_gone():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["science.research-entities"] == {"subdomains": ["research-entities-data"],
                                                                  "shapes": ["registry-records"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "research-entities-data" not in re.findall(r"^\| `([a-z0-9-]+)` \|", program, re.MULTILINE)
