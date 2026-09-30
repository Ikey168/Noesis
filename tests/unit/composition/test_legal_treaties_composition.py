"""The Legal bundle's legal.treaties provider and its optional treaties-untc / -eu / -coe features (#2636)."""

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
from tools.knowledge_engine_mcp.treaties import (
    TREATIES_SCOPES,
    TREATIES_TOOLS,
    TREATIES_WRITES,
)

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


def test_descriptor_validates_declares_constraints_stores_scopes_and_the_source_pack():
    descriptor = {d["id"]: d for d in provider_descriptors()}["legal.treaties"]
    assert validate_provider_descriptor(descriptor) == []
    capability = descriptor["capabilities"][0]
    assert capability["contract"] == {"name": "noesis-treaty-record", "version": "1.0.0"}
    assert {"records", "as_of", "statements", "minimisation", "identity", "links", "exclusions"} <= \
        set(capability["semantic_constraints"])
    assert "no legal advice" in capability["semantic_constraints"]["exclusions"]
    assert descriptor["readiness_probes"][0]["target"] == "treaty_revisions"
    assert descriptor["source_packs"] == [{"pack_id": "legal-research", "version": "1.5.0", "range": "^1.5.0"}]
    tools = {op["tool"].split(".", 1)[1]: op for op in descriptor["operations"]}
    assert set(tools) == TREATIES_TOOLS
    for name, op in tools.items():
        assert op["required_scopes"] == TREATIES_SCOPES[name]
        assert op["side_effect"] == ("local-mutation" if name in TREATIES_WRITES else "read-only")
    stores = {s["store"] for s in descriptor["stores"]}
    assert stores == {"src.kb.treaties_records", "src.kb.treaties_links"}


def test_the_bundle_gains_the_provider_with_three_default_off_features_and_no_new_pack():
    composition = json.loads((ROOT / "packs/legal/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(FEATURES) <= set(features) and all(features[f]["default"] is False for f in FEATURES)
    assert {"id": "legal.treaties", "version": "1.0.0"} in composition["contributes"]["providers"]
    assert validate_composition_manifest(adapt_all()["legal"]) == []
    plan = legal_plan()
    assert "legal.treaties" not in bound(plan)
    assert set(FEATURES) <= {o["feature"] for o in plan["omissions"] if o["pack"] == "legal"}
    pack = json.loads((ROOT / "packs/legal/pack.json").read_text())
    assert {"treaty-status-as-of", "treaty-reservations-and-objections"} <= set(pack["capabilities"])
    assert "interpretation of the legal effect of reservations" in pack["exclusions"]
    assert not list(ROOT.glob("packs/*treat*"))


def test_each_feature_binds_the_provider_independently_and_legislation_links_are_optional():
    for feature in FEATURES:
        plan = legal_plan([feature])
        assert plan["features"]["legal"] == [feature]
        assert {"legal.treaties", "platform.subscriptions"} <= bound(plan), feature
        assert "legal.sanctions" not in bound(plan)  # sanctions links degrade when absent
    assert {"pack_id": "legal-research", "version": "1.5.0", "range": "^1.1.0"} in \
        legal_plan(["treaties-untc"])["source_packs"]
    everything = legal_plan([*FEATURES, "sanctions", "courts"])
    assert sorted(everything["features"]["legal"]) == sorted([*FEATURES, "courts", "sanctions"])


def test_a_missing_consumed_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "ownership.core"]  # the identity state machine
    plan = legal_plan(["treaties-coe"], descriptors)
    assert plan["features"]["legal"] == []
    omission = next(o for o in plan["omissions"] if o["feature"] == "treaties-coe")
    assert "missing_contract" in omission["reason"]


def test_taxonomy_classifies_the_provider_and_the_gap_row_is_gone():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["legal.treaties"] == {"subdomains": ["treaties-international-law"],
                                                       "shapes": ["versioned-documents", "events-notices"]}
    roadmap = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `treaties-international-law` |" not in roadmap


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn, "treaties-untc") is False
    coordinator.select("legal", bundles["legal"]["version"], features=["treaties-untc"])
    assert coordinator.activate("legal-treaties-on")["status"] == "published"
    assert feature_enabled(conn, "treaties-untc") is True and feature_enabled(conn, "treaties-coe") is False
