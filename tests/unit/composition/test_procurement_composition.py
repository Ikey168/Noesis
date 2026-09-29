"""Public Procurement composition (P13): own provider plus funding.core, market.lei and shared providers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.readiness import CompositionView
from src.composition.shadow import SHADOW_REPORT, provider_descriptors
from src.domains import registry as domain_registry
from src.kb import funding_bundle, procurement_bundle
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "packs/procurement/manifest.json"
EXPECTED = {"procurement.core", "funding.core", "market.lei", "platform.research-projects", "platform.authored-reports",
            "platform.subscriptions", "platform.source-runtime"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def bound(plan, bundle):
    return {b["provider"] for b in plan["bindings"] if bundle in b["consumers"]}


def test_manifest_resolves_to_its_own_provider_plus_funding_lei_and_shared_providers():
    manifest = json.loads(MANIFEST.read_text())
    assert validate_composition_manifest(manifest) == []
    descriptor = next(d for d in provider_descriptors() if d["id"] == "procurement.core")
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["source_packs"] == [{"pack_id": "procurement", "version": "1.0.0", "range": "^1.0.0"}]
    _, coordinator, _, receipt = _migrated()
    assert receipt["status"] == "published"
    plan = coordinator.active()["plan"]
    assert bound(plan, "procurement") == EXPECTED
    funding = {b["capability"] for b in plan["bindings"] if b["provider"] == "funding.core"}
    assert {"funding.profiles", "funding.eligibility", "funding.shortlists", "funding.workspaces"} <= funding
    assert all({"funding-grants", "procurement"} <= set(b["consumers"]) for b in plan["bindings"]
               if b["capability"] in {"funding.eligibility", "funding.workspaces"})
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "procurement.core" for s in d["stores"]}
    assert not owned & others  # one authority per store
    assert {"pack_id": "procurement", "version": "1.0.0", "range": "^1.0.0"} in plan["source_packs"]


def test_disabling_procurement_is_a_selection_change_that_keeps_shared_providers_and_funding():
    conn, coordinator, _, _ = _migrated()
    assert procurement_bundle.is_enabled(conn, "procurement")
    status = procurement_bundle.readiness(conn, "procurement", scopes={"operator", "knowledge:procurement:read"})
    assert {o["provider"] for o in status["composition"]["operations"]} >= {"procurement.core", "funding.core"}
    result = procurement_bundle.set_enabled(conn, "procurement", False, principal_id="operator", scopes={"operator"})
    assert result["authority"] == "composition-coordinator" and result["enabled"] is False
    assert result["receipt"]["status"] == "published"  # the activation receipt of the selection change
    assert not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='procurement_bundle_state'").fetchone()
    plan = coordinator.active()["plan"]
    assert "procurement" not in {p["id"] for p in plan["packs"]}
    installed = {d["id"] for d in coordinator.installed("provider")}
    assert {"procurement.core", "funding.core", "market.lei", "platform.subscriptions"} <= installed
    assert funding_bundle.is_enabled(conn, "funding")
    assert bound(plan, "funding-grants") >= {"funding.core", "platform.research-projects", "platform.subscriptions"}
    assert bound(plan, "market") >= {"market.lei"}
    again = procurement_bundle.set_enabled(conn, "procurement", True, principal_id="operator", scopes={"operator"})
    assert again["enabled"] is True and bound(coordinator.active()["plan"], "procurement") == EXPECTED


def test_disabling_funding_keeps_procurement_bound_to_the_reused_funding_provider():
    conn, coordinator, _, _ = _migrated()
    funding_bundle.set_enabled(conn, "funding", False, principal_id="operator", scopes={"operator"})
    plan = coordinator.active()["plan"]
    assert "funding-grants" not in {p["id"] for p in plan["packs"]} and not funding_bundle.is_enabled(conn, "funding")
    assert procurement_bundle.is_enabled(conn, "procurement") and bound(plan, "procurement") == EXPECTED


def test_funding_tools_stay_attributed_to_funding_when_both_bundles_are_selected():
    _, coordinator, bundles, _ = _migrated()
    view = CompositionView(coordinator.active()["plan"], provider_descriptors(), bundles.values())
    assert view.tools["noesis-knowledge-engine.assess_funding_eligibility"].pack == "funding-grants"
    assert view.tools["noesis-knowledge-engine.assess_procurement_eligibility"].pack == "procurement"


def test_shadow_diff_covers_procurement_and_every_disagreement_is_annotated():
    report = json.loads(SHADOW_REPORT.read_text())
    assert "procurement" in report["bundles_resolved"] and report["disagreements"]["procurement"]
    assert all(item["annotation"] != "unreviewed" for item in report["disagreements"]["procurement"])
    assert not any("funding" in item["tool"] for item in report["disagreements"]["procurement"])
