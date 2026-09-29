"""The Political bundle's optional ``lobbying`` feature and the ``political.lobbying`` provider (#2009)."""

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
from src.kb.lobbying import feature_enabled
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "political.core",
    "political.lobbying",
    "ownership.core",
    "market.lei",
    "platform.entity-identity",
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


def political_plan(features=None, descriptors=None, bundles=None):
    bundles = bundles or adapt_all()
    root = {"pack": "political", "version": bundles["political"]["version"]}
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
    return {b["provider"] for b in plan["bindings"] if "political" in b["consumers"]}


def test_descriptor_declares_the_capability_operations_stores_probe_and_source_pack():
    descriptor = next(
        d for d in provider_descriptors() if d["id"] == "political.lobbying"
    )
    assert validate_provider_descriptor(descriptor) == []
    capability = descriptor["capabilities"][0]
    assert capability["id"] == "political.lobbying"
    assert capability["contract"] == {
        "name": "noesis-lobbying-record",
        "version": "1.0.0",
    }
    tools = {o["tool"].rsplit(".", 1)[1] for o in descriptor["operations"]}
    assert {
        "list_dossier_declared_interests",
        "list_registrant_declarations",
        "list_official_meetings",
        "review_lobbying_identity_match",
        "revert_lobbying_identity_match",
        "review_lobbying_dossier_link",
        "revert_lobbying_dossier_link",
    } <= tools
    assert {s["store"] for s in descriptor["stores"]} == {
        "src.kb.lobbying",
        "src.kb.lobbying_links",
    }
    assert all(
        s["revision_addressable"] and s["namespace_scoped"]
        for s in descriptor["stores"]
    )
    assert descriptor["readiness_probes"] == [
        {"id": "revisions", "kind": "table-exists", "target": "lobbying_revisions"}
    ]
    assert descriptor["source_packs"] == [
        {"pack_id": "official-political-records", "version": "1.1.0", "range": "^1.1.0"}
    ]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "political.lobbying"
        for s in d["stores"]
    }
    assert (
        not owned & others
    )  # no second entity, project, subscription or permission store


def test_the_bundle_resolves_with_the_feature_off_by_default():
    composition = json.loads((ROOT / "packs/political/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(features) == {
        "lobbying",
        "elections",
        "legislation-us",
        "legislation-uk",
    }  # independent optional features (#1911, #1908, #2208)
    feature = features["lobbying"]
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == {
        "political.lobbying",
        "political.knowledge",
        "platform.entity-identity",
        "market.legal-entities",
        "ownership.identity",
        "platform.subscriptions",
    }
    assert validate_composition_manifest(adapt_all()["political"]) == []
    plan = political_plan()
    assert plan["features"]["political"] == [] and bound(plan) == {"political.core"}
    assert {
        "pack": "political",
        "feature": "lobbying",
        "reason": "not selected",
    } in plan["omissions"]


def test_selecting_the_feature_binds_its_provider_and_the_consumed_ones():
    plan = political_plan(["lobbying"])
    assert (
        plan["features"]["political"] == ["lobbying"]
        and bound(plan) == FEATURE_PROVIDERS
    )
    # Only the other optional features, left unselected, are omitted (elections #1908, legislation-uk/-us
    # #2208), plus the consumed Market bafin-notices and Corporate Ownership bafin-voting-rights features
    # (#2106), also left unselected.
    assert sorted(plan["omissions"], key=lambda o: o["feature"]) == [
        {"pack": "market", "feature": "bafin-notices", "reason": "not selected"},
        {
            "pack": "corporate-ownership",
            "feature": "bafin-voting-rights",
            "reason": "not selected",
        },
        {"pack": "political", "feature": "elections", "reason": "not selected"},
        {"pack": "political", "feature": "legislation-uk", "reason": "not selected"},
        {"pack": "political", "feature": "legislation-us", "reason": "not selected"},
    ]
    # The bundle ships official-political-records 1.3.0 (elections result sources #1908, legislation sources
    # #2208); the lobbying descriptor's ^1.1.0 range is satisfied by it.
    assert {
        "pack_id": "official-political-records",
        "version": "1.3.0",
        "range": "^1.3.0",
    } in plan["source_packs"]
    profiles = {p["id"]: p for p in adapt_all()["political"]["contributes"]["profiles"]}
    defaults = profiles["political.lobbying-review"]["workflow_defaults"]
    assert set(defaults) >= {"registers", "as_of", "unmatched_names"}


def test_a_missing_consumed_provider_is_a_visible_omission_not_a_failure():
    descriptors = [d for d in provider_descriptors() if d["id"] != "market.lei"]
    plan = political_plan(["lobbying"], descriptors)
    assert plan["features"]["political"] == [] and bound(plan) == {"political.core"}
    omission = next(o for o in plan["omissions"] if o["feature"] == "lobbying")
    assert (
        omission["capability"] == "market.legal-entities"
        and "missing_contract" in omission["reason"]
    )


def test_the_feature_coexists_with_the_optional_elections_feature():
    """Lobbying and elections are selected independently or together (the real elections feature, #1908)."""
    bundles = adapt_all()
    assert validate_composition_manifest(bundles["political"]) == []
    for selection in ([], ["lobbying"], ["elections"], ["elections", "lobbying"]):
        plan = political_plan(selection, bundles=bundles)
        assert sorted(plan["features"]["political"]) == sorted(selection)
        assert ("political.lobbying" in bound(plan)) == ("lobbying" in selection)
        assert ("political.elections" in bound(plan)) == ("elections" in selection)


def test_no_new_pack_directory_or_enablement_flag_is_added():
    pack = json.loads((ROOT / "packs/political/pack.json").read_text())
    assert {"lobbying-register-declarations", "declared-spend-ranges"} <= set(
        pack["capabilities"]
    )
    assert pack["schema_versions"]["lobbying-record"] == "1.0.0"
    assert {
        "influence or corruption claims",
        "undeclared-lobbying inference",
        "point estimates, totals or averages of declared spend ranges",
    } <= set(pack["exclusions"])
    assert not list(ROOT.glob("packs/*lobby*"))
    assert "lobbying" not in json.loads(
        (ROOT / "config/domain_packs.json").read_text()
    ).get("enabled_packs", [])


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select(
        "political", bundles["political"]["version"], features=["lobbying"]
    )
    receipt = coordinator.activate("political-lobbying-on")
    assert receipt["status"] == "published" and feature_enabled(conn) is True
    coordinator.select("political", bundles["political"]["version"], features=[])
    coordinator.activate("political-lobbying-off")
    assert feature_enabled(conn) is False
