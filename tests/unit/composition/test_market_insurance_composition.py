"""The Market bundle's optional ``insurance`` feature and the ``market.insurance`` provider (#2230, IN11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import satisfies, validate_composition_manifest, validate_provider_descriptor
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.domains.market.insurance import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.market_mcp.insurance import INSURANCE_TOOLS

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "market.core",
    "market.lei",
    "market.insurance",
    "platform.entity-identity",
    "platform.subscriptions",
    "platform.source-runtime",
}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def plan_for(roots, descriptors=None):
    bundles = adapt_all()
    result = resolve(
        [{"pack": pack, "version": bundles[pack]["version"], **({"features": f} if f is not None else {})}
         for pack, f in roots],
        list(bundles.values()),
        descriptors if descriptors is not None else provider_descriptors(),
    )
    assert result.ok, result.failure
    return result.plan


def bound(plan, consumer):
    return {b["provider"] for b in plan["bindings"] if consumer in b["consumers"]}


def test_descriptor_declares_read_only_lookups_readiness_store_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "market.insurance")
    assert validate_provider_descriptor(descriptor) == []
    capability = descriptor["capabilities"][0]
    assert capability["contract"] == {"name": "noesis-insurance-record", "version": "1.0.0"}
    assert "no solvency or rating assessment" in capability["semantic_constraints"]["exclusions"]
    tools = {o["tool"] for o in descriptor["operations"]}
    assert {"noesis-market.insurance_insurer_as_of", "noesis-market.insurance_market_as_of",
            "noesis-market.insurance_event_estimates", "noesis-market.insurance_revision_history"} <= tools
    assert all(o["side_effect"] == "read-only" for o in descriptor["operations"])
    assert {t.split(".", 1)[1] for t in tools} <= INSURANCE_TOOLS
    assert descriptor["readiness_probes"] == [
        {"id": "records", "kind": "table-exists", "target": "insurance_record_revisions"}]
    installed = json.loads((ROOT / "config/source_packs/market-insurance.json").read_text())
    assert installed["pack_id"] == "insurance-supervisory-and-catastrophe-losses"
    assert satisfies(installed["version"], "^1.0.0")
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "market.insurance" for s in d["stores"]}
    assert owned == {"insurance-record"} and not owned & others


def test_the_feature_is_off_by_default_and_the_bundle_resolves_unchanged():
    composition = json.loads((ROOT / "packs/market/composition.json").read_text())
    feature = next(f for f in composition["optional_features"] if f["id"] == "insurance")
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == {
        "market.insurance", "market.legal-entities", "platform.entity-identity", "platform.subscriptions",
        "platform.source-acquisition"}
    assert validate_composition_manifest(adapt_all()["market"]) == []
    plan = plan_for([("market", None)])
    assert plan["features"]["market"] == [] and bound(plan, "market") == {"market.core", "market.lei"}
    assert {"pack": "market", "feature": "insurance", "reason": "not selected"} in plan["omissions"]
    off = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert not {f"noesis-market.{t}" for t in INSURANCE_TOOLS} & set(off.tools)


def test_selecting_the_feature_binds_its_providers_and_exposes_its_tools():
    plan = plan_for([("market", ["insurance"])])
    assert plan["features"]["market"] == ["insurance"] and bound(plan, "market") == FEATURE_PROVIDERS
    assert {"pack_id": "insurance-supervisory-and-catastrophe-losses", "version": "1.0.0",
            "range": "^1.0.0"} in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert {"noesis-market.insurance_insurer_as_of", "noesis-market.insurance_event_estimates"} <= set(view.tools)


def test_a_missing_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "platform.subscriptions"]
    plan = plan_for([("market", ["insurance"])], descriptors)
    omission = next(o for o in plan["omissions"] if o["feature"] == "insurance")
    assert omission["capability"] == "platform.subscriptions" and "missing_contract" in omission["reason"]


def test_v1_pack_manifest_is_unchanged_and_no_new_pack_is_added():
    assert "insurance" not in (ROOT / "packs/market/pack.json").read_text().lower()
    assert not list(ROOT.glob("packs/*insurance*"))


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("market", bundles["market"]["version"], features=["insurance"])
    assert coordinator.activate("market-insurance-on")["status"] == "published"
    assert feature_enabled(conn) is True
