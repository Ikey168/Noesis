"""The Legal bundle's optional ``sanctions`` feature and the ``legal.sanctions`` provider (#1980)."""

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
from src.kb.sanctions import feature_enabled
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "legal.core",
    "legal.sanctions",
    "ownership.core",
    "market.lei",
    "platform.entity-identity",
    "economics.core",
    "platform.subscriptions",
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


def test_descriptor_declares_the_capability_stores_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "legal.sanctions")
    assert validate_provider_descriptor(descriptor) == []
    capability = descriptor["capabilities"][0]
    assert capability["id"] == "legal.sanctions" and capability["contract"] == {
        "name": "noesis-sanctions-record",
        "version": "1.0.0",
    }
    assert {o["tool"].rsplit(".", 1)[1] for o in descriptor["operations"]} >= {
        "lookup_sanctions_designation",
        "designation_history_as_of",
        "control_list_entry_as_of",
    }
    assert all(o["side_effect"] == "read-only" for o in descriptor["operations"])
    store = descriptor["stores"][0]
    assert (
        store["store"] == "src.kb.sanctions"
        and store["revision_addressable"]
        and store["namespace_scoped"]
    )
    assert descriptor["readiness_probes"] == [
        {
            "id": "designations",
            "kind": "table-exists",
            "target": "sanctions_designations",
        }
    ]
    assert descriptor["source_packs"] == [
        {"pack_id": "legal-research", "version": "1.2.0", "range": "^1.1.0"}
    ]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "legal.sanctions"
        for s in d["stores"]
    }
    assert not owned & others


def test_the_bundle_resolves_with_the_feature_off_by_default():
    composition = json.loads((ROOT / "packs/legal/composition.json").read_text())
    feature = composition["optional_features"][0]
    assert feature["id"] == "sanctions" and feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == {
        "legal.sanctions",
        "legal.works",
        "ownership.identity",
        "market.legal-entities",
        "platform.entity-identity",
        "economics.knowledge",
        "platform.subscriptions",
    }
    assert validate_composition_manifest(adapt_all()["legal"]) == []
    plan = legal_plan()
    assert plan["features"]["legal"] == [] and bound(plan) == {"legal.core"}
    assert {"pack": "legal", "feature": "sanctions", "reason": "not selected"} in plan[
        "omissions"
    ]


def test_selecting_the_feature_binds_its_provider_and_the_consumed_ones():
    plan = legal_plan(["sanctions"])
    assert (
        plan["features"]["legal"] == ["sanctions"] and bound(plan) == FEATURE_PROVIDERS
    )
    # The consumed Economics bundle is pulled in with its own optional public-finance (#1909) and demographics
    # (#1914) features left unselected; nothing of the sanctions feature is omitted.
    assert sorted(plan["omissions"], key=lambda o: o["feature"]) == [
        {"pack": "economics", "feature": "demographics", "reason": "not selected"},
        {"pack": "economics", "feature": "public-finance", "reason": "not selected"},
    ]
    assert {"pack_id": "legal-research", "version": "1.2.0", "range": "^1.1.0"} in plan[
        "source_packs"
    ]


def test_a_missing_consumed_provider_is_a_visible_omission_not_a_failure():
    descriptors = [d for d in provider_descriptors() if d["id"] != "ownership.core"]
    plan = legal_plan(["sanctions"], descriptors)
    assert plan["features"]["legal"] == [] and bound(plan) == {"legal.core"}
    omission = next(o for o in plan["omissions"] if o["feature"] == "sanctions")
    assert (
        omission["capability"] == "ownership.identity"
        and "missing_contract" in omission["reason"]
    )


def test_no_new_pack_directory_or_enablement_flag_is_added():
    pack = json.loads((ROOT / "packs/legal/pack.json").read_text())
    assert {"sanctions-designations", "dual-use-control-list-editions"} <= set(
        pack["capabilities"]
    )
    assert pack["schema_versions"]["sanctions-record"] == "1.0.0"
    assert {
        "screening verdicts",
        "sanctions or AML compliance determinations",
        "legal advice",
    } <= set(pack["exclusions"])
    assert not list(ROOT.glob("packs/*sanction*"))


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("legal", bundles["legal"]["version"], features=["sanctions"])
    receipt = coordinator.activate("legal-sanctions-on")
    assert receipt["status"] == "published" and feature_enabled(conn) is True
    coordinator.select("legal", bundles["legal"]["version"], features=[])
    coordinator.activate("legal-sanctions-off")
    assert feature_enabled(conn) is False
