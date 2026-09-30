"""The Legal bundle's optional treaties features and the legal.treaties provider (#2636)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.treaties_records import feature_enabled
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURES = ("treaties-untc", "treaties-eu", "treaties-coe")


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def legal_plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "legal", "version": bundles["legal"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()),
                     descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "legal" in b["consumers"]}


def test_the_descriptor_declares_its_capability_constraints_stores_and_source_pack():
    descriptor = {d["id"]: d for d in provider_descriptors()}["legal.treaties"]
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["capabilities"][0]["contract"] == {"name": "noesis-treaty-record", "version": "1.0.0"}
    assert descriptor["readiness_probes"][0]["target"] == "treaty_revisions"
    assert descriptor["source_packs"] == [{"pack_id": "legal-research", "version": "1.5.0", "range": "^1.5.0"}]
    constraints = descriptor["capabilities"][0]["semantic_constraints"]
    assert {"exclusions", "minimisation", "identity", "links", "as_of"} <= set(constraints)
    assert "no legal advice" in constraints["exclusions"]
    assert {s["store"] for s in descriptor["stores"]} == {"src.kb.treaties_store", "src.kb.treaties_identity",
                                                          "src.kb.treaties_links"}


def test_no_new_pack_and_each_source_is_a_separate_optional_feature():
    composition = json.loads((ROOT / "packs/legal/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(FEATURES) <= set(features)
    assert all(features[f]["default"] is False for f in FEATURES)
    assert all(features[f]["requires"][0]["capability"] == "legal.treaties" for f in FEATURES)
    required = {r["capability"] for f in FEATURES for r in features[f]["requires"]}
    assert "legal.sanctions" not in required and "legal.works" not in required  # links degrade instead
    assert {"id": "legal.treaties", "version": "1.0.0"} in composition["contributes"]["providers"]
    assert validate_composition_manifest(adapt_all()["legal"]) == []
    assert not list(ROOT.glob("packs/*treat*"))
    plan = legal_plan()
    assert plan["features"]["legal"] == [] and "legal.treaties" not in bound(plan)
    assert set(FEATURES) <= {o["feature"] for o in plan["omissions"] if o["pack"] == "legal"}
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["legal.treaties"] == {"subdomains": ["treaties-international-law"],
                                                       "shapes": ["versioned-documents", "events-notices"]}
    assert "`treaties-international-law`" not in (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()


def test_selecting_a_feature_binds_the_provider_and_the_consumed_ones():
    plan = legal_plan(["treaties-coe"])
    assert plan["features"]["legal"] == ["treaties-coe"]
    assert {"legal.core", "legal.treaties", "platform.subscriptions"} <= bound(plan)
    assert "legal.sanctions" not in bound(plan)
    assert {"pack_id": "legal-research", "version": "1.5.0", "range": "^1.1.0"} in plan["source_packs"]
    every = legal_plan(list(FEATURES) + ["sanctions", "courts"])
    assert set(every["features"]["legal"]) == set(FEATURES) | {"sanctions", "courts"}


def test_a_missing_consumed_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "platform.entity-identity"]
    plan = legal_plan(["treaties-eu"], descriptors)
    assert plan["features"]["legal"] == []
    omission = next(o for o in plan["omissions"] if o["feature"] == "treaties-eu")
    assert "missing_contract" in omission["reason"]


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn, "treaties-eu") is False
    coordinator.select("legal", bundles["legal"]["version"], features=["treaties-eu"])
    assert coordinator.activate("legal-treaties-on")["status"] == "published"
    assert feature_enabled(conn, "treaties-eu") is True and feature_enabled(conn, "treaties-coe") is False
