"""The Science bundle's optional ``research-entities-*`` features and the ``science.research-entities`` provider
(RE12, #2639)."""

from __future__ import annotations

import json
from pathlib import Path

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
from src.kb.research_entities_records import FEATURES, feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.research_entities import RESEARCH_ENTITY_TOOLS

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "research-discovery", "version": "1.5.0", "range": "^1.5.0"}
FEATURE_IDS = sorted(FEATURES.values())


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


def test_descriptor_declares_capability_constraints_operations_stores_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "science.research-entities")
    assert validate_provider_descriptor(descriptor) == []
    raw = json.loads((ROOT / "packs/science/providers/science.research-entities.json").read_text())
    assert validate_provider_descriptor(raw) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-research-entity-record", "version": "1.0.0"}
    constraints = capability["semantic_constraints"]
    assert set(constraints) == {"records", "revisions", "minimisation", "identity", "links", "exclusions"}
    assert "no researcher rankings or metrics" in constraints["exclusions"]
    assert "author disambiguation by name" in constraints["exclusions"]
    assert {o["tool"].split(".", 1)[1] for o in descriptor["operations"]} == RESEARCH_ENTITY_TOOLS
    assert descriptor["readiness_probes"] == [{"id": "revisions", "kind": "table-exists",
                                               "target": "rentity_revisions"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != descriptor["id"] for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("rentity_") for s in descriptor["stores"] for t in s["tables"])


def test_four_separate_optional_features_off_by_default_in_the_existing_bundle():
    composition = json.loads((ROOT / "packs/science/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(FEATURE_IDS) <= set(features)
    for feature in FEATURE_IDS:
        assert features[feature]["default"] is False
        assert {r["capability"] for r in features[feature]["requires"]} == {
            "science.research-entities", "platform.entity-identity", "platform.subscriptions",
            "platform.source-acquisition"}
    assert {"id": "science.research-entities", "version": "1.0.0"} in composition["contributes"]["providers"]
    assert validate_composition_manifest(adapt_all()["science"]) == []
    assert not (ROOT / "packs/research-entities").exists()
    pack = json.loads((ROOT / "packs/science/pack.json").read_text())
    assert "researcher rankings or metrics" in pack["exclusions"]
    assert pack["schema_versions"]["research-entity-record"] == "1.0.0"
    manifest = json.loads((ROOT / "config/source_packs/research.json").read_text())
    assert manifest["version"] == "1.5.0"
    assert {s["source_id"] for s in manifest["sources"] if s.get("connector") == "research-entities"} == {
        "ror-organisations", "orcid-public-records", "datacite-research-datasets", "cordis-horizon-projects"}
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["science.research-entities"] == {"subdomains": ["research-entities-data"],
                                                                   "shapes": ["registry-records"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `research-entities-data` |" not in program


@pytest.mark.parametrize("selection", [[], ["research-entities-ror"], ["research-entities-orcid"], FEATURE_IDS])
def test_each_selection_resolves_and_off_keeps_todays_bindings(selection):
    plan = science_plan(selection)
    assert sorted(plan["features"]["science"]) == sorted(selection)
    on = bool(selection)
    assert ("science.research-entities" in bound(plan)) == on
    if on:
        assert {"platform.subscriptions", "platform.source-runtime"} <= bound(plan)
        # The bundle pins the research-discovery release that carries the research-entity sources.
        assert {"pack_id": "research-discovery", "version": "1.5.0", "range": "^1.4.0"} in plan["source_packs"]
        bindings = [b for b in plan["bindings"] if "science" in b["consumers"]]
        assert len({b["capability"] for b in bindings}) == len(bindings)
    else:
        assert bound(plan) == bound(science_plan([]))
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.research_organisation_as_of" in view.tools) == on


def test_a_missing_consumed_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "platform.subscriptions"]
    plan = science_plan(["research-entities-ror"], descriptors)
    assert plan["features"]["science"] == []
    omission = next(o for o in plan["omissions"] if o["feature"] == "research-entities-ror")
    assert omission["capability"] == "platform.subscriptions"


def test_feature_enablement_follows_the_active_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("science", bundles["science"]["version"], features=["research-entities-cordis"])
    assert coordinator.activate("science-research-entities-on")["status"] == "published"
    assert feature_enabled(conn) is True and feature_enabled(conn, "research-entities-cordis") is True
    assert feature_enabled(conn, "research-entities-orcid") is False
    coordinator.select("science", bundles["science"]["version"], features=[])
    coordinator.activate("science-research-entities-off")
    assert feature_enabled(conn) is False
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "`research-entities-orcid`" in doc and "`noesis-research-entity-record-v1`" in doc
