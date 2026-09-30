"""The Legal bundle's optional regulatory enforcement features and the legal.enforcement provider (#2710, EN12)."""

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


def test_the_descriptor_validates_and_declares_constraints_stores_and_the_source_pack():
    descriptor = {d["id"]: d for d in provider_descriptors()}["legal.enforcement"]
    assert validate_provider_descriptor(descriptor) == []
    capability = descriptor["capabilities"][0]
    assert capability["contract"] == {"name": "noesis-enforcement-record", "version": "1.0.0"}
    assert {"records", "outcomes", "penalties", "minimisation", "identity", "links", "exclusions"} <= set(
        capability["semantic_constraints"])
    assert "no risk or compliance scoring" in capability["semantic_constraints"]["exclusions"]
    assert descriptor["readiness_probes"][0]["target"] == "enforcement_records"
    assert descriptor["source_packs"] == [{"pack_id": "legal-research", "version": "1.5.0", "range": "^1.5.0"}]
    from tools.knowledge_engine_mcp.enforcement import (
        ENFORCEMENT_SCOPES,
        ENFORCEMENT_WRITES,
    )

    operations = {o["tool"].split(".", 1)[1]: o for o in descriptor["operations"]}
    assert set(operations) == set(ENFORCEMENT_SCOPES)
    for tool, operation in operations.items():
        assert operation["required_scopes"] == ENFORCEMENT_SCOPES[tool]
        assert (operation["side_effect"] == "local-mutation") == (tool in ENFORCEMENT_WRITES)


def test_no_new_pack_and_four_independent_features_default_off():
    composition = json.loads((ROOT / "packs/legal/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(FEATURES) <= set(features)
    assert all(features[f]["default"] is False for f in FEATURES)
    assert "legal.enforcement" in {p["id"] for p in composition["contributes"]["providers"]}
    assert validate_composition_manifest(adapt_all()["legal"]) == []
    plan = legal_plan()
    assert plan["features"]["legal"] == [] and "legal.enforcement" not in bound(plan)
    assert set(FEATURES) <= {o["feature"] for o in plan["omissions"] if o["pack"] == "legal"}
    assert not list(ROOT.glob("packs/*enforcement*"))
    pack = json.loads((ROOT / "packs/legal/pack.json").read_text())
    assert "regulatory-enforcement-actions" in pack["capabilities"]
    assert "treating settled 'neither admit nor deny' outcomes as findings" in pack["exclusions"]


def test_each_feature_binds_the_provider_and_ownership_market_and_courts_stay_optional():
    for feature in FEATURES:
        plan = legal_plan([feature])
        assert plan["features"]["legal"] == [feature]
        providers = bound(plan)
        assert {"legal.core", "legal.enforcement", "platform.subscriptions"} <= providers
        # Ownership, competition, market and court links degrade gracefully: the features do not require them.
        assert not {"ownership.core", "legal.courts", "market.core", "market.lei"} & providers
    both = legal_plan(["enforcement-sec", "enforcement-edpb", "courts"])
    assert sorted(both["features"]["legal"]) == ["courts", "enforcement-edpb", "enforcement-sec"]


def test_taxonomy_classifies_the_provider_and_the_gap_row_is_gone():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["legal.enforcement"] == {
        "subdomains": ["regulatory-enforcement"], "shapes": ["events-notices", "versioned-documents"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `regulatory-enforcement` |" not in program


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("legal", bundles["legal"]["version"], features=["enforcement-fca"])
    assert coordinator.activate("legal-enforcement-on")["status"] == "published"
    assert feature_enabled(conn, "enforcement-fca") is True and feature_enabled(conn, "enforcement-sec") is False
    assert feature_enabled(conn) is True
