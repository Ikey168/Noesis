"""The Legal bundle's optional ``federal-statutes`` feature and the ``legal.federal-statutes`` provider (#2117)."""

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
from src.kb.legal_federal import feature_enabled
from src.kb.sanctions import feature_enabled as sanctions_enabled
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "legal.core",
    "legal.federal-statutes",
    "political.core",
    "platform.subscriptions",
    "platform.source-runtime",
}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (
        dict(domain_registry._REGISTRY),
        set(domain_registry._ENABLED),
        domain_registry._AUTHORITY,
    )
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
    result = resolve(
        [root],
        list(bundles.values()),
        descriptors if descriptors is not None else provider_descriptors(),
    )
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "legal" in b["consumers"]}


def test_descriptor_declares_the_capability_store_probe_and_source_pack():
    descriptor = next(
        d for d in provider_descriptors() if d["id"] == "legal.federal-statutes"
    )
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["capabilities"][0]["contract"] == {
        "name": "noesis-legal-federal-statute",
        "version": "1.0.0",
    }
    assert {o["tool"].rsplit(".", 1)[1] for o in descriptor["operations"]} >= {
        "get_federal_provision",
        "compare_provision_versions",
        "list_amendment_acts",
        "decisions_citing_provision",
        "resolve_statutory_citation",
    }
    assert all(o["side_effect"] == "read-only" for o in descriptor["operations"])
    assert descriptor["readiness_probes"] == [
        {
            "id": "statute-versions",
            "kind": "table-exists",
            "target": "legal_statute_versions",
        }
    ]
    assert descriptor["source_packs"] == [
        {"pack_id": "legal-research", "version": "1.3.0", "range": "^1.3.0"}
    ]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "legal.federal-statutes"
        for s in d["stores"]
    }
    assert not owned & others


def test_the_bundle_resolves_with_the_feature_off_by_default():
    composition = json.loads((ROOT / "packs/legal/composition.json").read_text())
    feature = next(
        f for f in composition["optional_features"] if f["id"] == "federal-statutes"
    )
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == {
        "legal.federal-statutes",
        "legal.works",
        "political.knowledge",
        "platform.subscriptions",
        "platform.source-acquisition",
    }
    assert validate_composition_manifest(adapt_all()["legal"]) == []
    plan = legal_plan()
    assert plan["features"]["legal"] == [] and bound(plan) == {"legal.core"}
    assert {
        "pack": "legal",
        "feature": "federal-statutes",
        "reason": "not selected",
    } in plan["omissions"]


def test_selecting_the_feature_binds_its_provider_and_composes_with_sanctions():
    plan = legal_plan(["federal-statutes"])
    assert (
        plan["features"]["legal"] == ["federal-statutes"]
        and bound(plan) == FEATURE_PROVIDERS
    )
    assert not [
        o
        for o in plan["omissions"]
        if o["pack"] == "legal" and o["feature"] == "federal-statutes"
    ]
    assert {"pack_id": "legal-research", "version": "1.5.0", "range": "^1.1.0"} in plan[
        "source_packs"
    ]
    both = legal_plan(["federal-statutes", "sanctions"])
    assert sorted(both["features"]["legal"]) == ["federal-statutes", "sanctions"]
    assert {"legal.sanctions", "legal.federal-statutes"} <= bound(both)


def test_a_missing_consumed_provider_is_a_visible_omission_not_a_failure():
    descriptors = [d for d in provider_descriptors() if d["id"] != "political.core"]
    plan = legal_plan(["federal-statutes"], descriptors)
    assert plan["features"]["legal"] == [] and bound(plan) == {"legal.core"}
    omission = next(o for o in plan["omissions"] if o["feature"] == "federal-statutes")
    assert (
        omission["capability"] == "political.knowledge"
        and "missing_contract" in omission["reason"]
    )


def test_v1_pack_manifest_is_unchanged_and_no_new_pack_is_added():
    pack = json.loads((ROOT / "packs/legal/pack.json").read_text())
    text = json.dumps(pack)
    assert "federal-statutes" not in text and "get_federal_provision" not in text
    assert not list(ROOT.glob("packs/*statute*"))


def test_feature_enablement_follows_the_active_composition_selection_and_is_independent_of_sanctions():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select(
        "legal", bundles["legal"]["version"], features=["federal-statutes"]
    )
    assert coordinator.activate("legal-federal-on")["status"] == "published"
    assert feature_enabled(conn) is True and sanctions_enabled(conn) is False
    coordinator.select("legal", bundles["legal"]["version"], features=[])
    coordinator.activate("legal-federal-off")
    assert feature_enabled(conn) is False
