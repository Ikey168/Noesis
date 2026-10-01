"""The Legal bundle's legal.enforcement provider and its four optional features (#2651, EN12)."""

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
from src.kb.enforcement import FEATURES, feature_enabled
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
ENFORCEMENT_FEATURES = ("enforcement-sec", "enforcement-fca", "enforcement-epa", "enforcement-edpb")


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


def test_descriptor_declares_constraints_stores_and_the_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "legal.enforcement")
    assert validate_provider_descriptor(descriptor) == []
    capability = descriptor["capabilities"][0]
    assert capability["contract"] == {"name": "noesis-enforcement-action", "version": "1.0.0"}
    assert {"outcomes", "penalties", "minimisation", "identity", "links", "exclusions"} <= set(
        capability["semantic_constraints"])
    assert "never summed" in capability["semantic_constraints"]["penalties"] or \
        "summed" in capability["semantic_constraints"]["penalties"]
    assert descriptor["readiness_probes"][0]["target"] == "enforcement_records"
    assert descriptor["source_packs"] == [{"pack_id": "legal-research", "version": "1.5.0", "range": "^1.5.0"}]


def test_no_new_pack_and_four_independent_features_off_by_default():
    composition = json.loads((ROOT / "packs/legal/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(ENFORCEMENT_FEATURES) <= set(features) and set(FEATURES.values()) == set(ENFORCEMENT_FEATURES)
    assert all(features[f]["default"] is False for f in ENFORCEMENT_FEATURES)
    for feature in ENFORCEMENT_FEATURES:
        required = {r["capability"] for r in features[feature]["requires"]}
        # Ownership, market and court links degrade gracefully: the features never require those packs.
        assert not required & {"ownership.identity", "market.lei", "legal.courts", "market.instruments"}
    assert validate_composition_manifest(adapt_all()["legal"]) == []
    plan = legal_plan()
    assert "legal.enforcement" not in bound(plan)
    omitted = {o["feature"] for o in plan["omissions"] if o["pack"] == "legal"}
    assert set(ENFORCEMENT_FEATURES) <= omitted
    assert not list(ROOT.glob("packs/*enforcement*"))
    pack = json.loads((ROOT / "packs/legal/pack.json").read_text())
    assert {"enforcement-actions-as-published", "enforcement-monitoring"} <= set(pack["capabilities"])
    assert "risk or compliance scoring of respondents" in pack["exclusions"]


def test_each_feature_binds_the_provider_alone_and_without_ownership():
    for feature in ENFORCEMENT_FEATURES:
        plan = legal_plan([feature])
        assert plan["features"]["legal"] == [feature]
        assert {"legal.core", "legal.enforcement", "platform.subscriptions"} <= bound(plan)
    all_on = legal_plan(list(ENFORCEMENT_FEATURES) + ["courts"])
    assert sorted(all_on["features"]["legal"]) == sorted([*ENFORCEMENT_FEATURES, "courts"])
    assert {"pack_id": "legal-research", "version": "1.5.0", "range": "^1.1.0"} in all_on["source_packs"]
    without_ownership = [d for d in provider_descriptors() if not d["id"].startswith("ownership.")]
    plan = legal_plan(["enforcement-sec"], without_ownership)
    assert plan["features"]["legal"] == ["enforcement-sec"]


def test_a_missing_consumed_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "platform.subscriptions"]
    plan = legal_plan(["enforcement-fca"], descriptors)
    assert plan["features"]["legal"] == []
    omission = next(o for o in plan["omissions"] if o["feature"] == "enforcement-fca")
    assert "missing_contract" in omission["reason"]


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn, "enforcement-sec") is False
    coordinator.select("legal", bundles["legal"]["version"], features=["enforcement-sec", "enforcement-edpb"])
    assert coordinator.activate("legal-enforcement-on")["status"] == "published"
    assert feature_enabled(conn, "enforcement-sec") and feature_enabled(conn, "enforcement-edpb")
    assert feature_enabled(conn, "enforcement-fca") is False
