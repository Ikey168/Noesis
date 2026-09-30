"""The Market bundle's optional ``bafin-notices`` feature, the ``market.bafin`` provider and the Corporate Ownership
``bafin-voting-rights`` feature that composes it into the ownership graph (#2106, BF11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    satisfies,
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors

from src.composition.readiness import CompositionView
from src.domains import registry as domain_registry
from src.domains.market.bafin_notices import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.bafin_notices import BAFIN_TOOLS

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "market.core",
    "market.lei",
    "market.bafin",
    "platform.entity-identity",
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


def plan_for(roots, descriptors=None):
    bundles = adapt_all()
    result = resolve(
        [
            {
                "pack": pack,
                "version": bundles[pack]["version"],
                **({"features": f} if f is not None else {}),
            }
            for pack, f in roots
        ],
        list(bundles.values()),
        descriptors if descriptors is not None else provider_descriptors(),
    )
    assert result.ok, result.failure
    return result.plan


def bound(plan, consumer):
    return {b["provider"] for b in plan["bindings"] if consumer in b["consumers"]}


def test_descriptor_declares_capability_store_probe_scopes_source_pack_and_ownership_composition():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "market.bafin")
    assert validate_provider_descriptor(descriptor) == []
    capability = descriptor["capabilities"][0]
    assert capability["contract"] == {"name": "noesis-bafin-notice", "version": "1.0.0"}
    assert "ownership.records" in capability["semantic_constraints"]["composition"]
    assert "no investment advice" in capability["semantic_constraints"]["exclusions"]
    tools = {o["tool"].rsplit(".", 1)[1]: o for o in descriptor["operations"]}
    assert {
        "bafin_holders_as_of",
        "bafin_managers_transactions",
        "bafin_net_short_positions",
        "bafin_warnings_for_entity",
        "bafin_notice_dossier",
    } <= set(tools)
    assert tools["project_bafin_voting_rights"]["side_effect"] == "local-mutation"
    assert all(
        o["side_effect"] == "read-only"
        for n, o in tools.items()
        if n != "project_bafin_voting_rights"
    )
    assert descriptor["readiness_probes"] == [
        {"id": "notices", "kind": "table-exists", "target": "bafin_notice_revisions"}
    ]
    assert descriptor["source_packs"] == [
        {
            "pack_id": "bafin-capital-market-notices",
            "version": "1.0.0",
            "range": "^1.0.0",
        }
    ]
    installed = json.loads((ROOT / "config/source_packs/market-bafin.json").read_text())
    assert installed["pack_id"] == "bafin-capital-market-notices" and satisfies(
        installed["version"], "^1.0.0"
    )
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "market.bafin"
        for s in d["stores"]
    }
    assert owned == {"bafin-notice"} and not owned & others


def test_the_market_bundle_resolves_unchanged_with_the_feature_off_by_default():
    composition = json.loads((ROOT / "packs/market/composition.json").read_text())
    feature = next(
        f for f in composition["optional_features"] if f["id"] == "bafin-notices"
    )
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == {
        "market.bafin-notices",
        "market.instruments",
        "market.legal-entities",
        "platform.entity-identity",
        "platform.subscriptions",
        "platform.source-acquisition",
    }
    assert validate_composition_manifest(adapt_all()["market"]) == []
    plan = plan_for([("market", None)])
    assert plan["features"]["market"] == [] and bound(plan, "market") == {
        "market.core",
        "market.lei",
    }
    assert plan["omissions"] == [
        {"pack": "market", "feature": "bafin-notices", "reason": "not selected"},
        {"pack": "market", "feature": "insurance", "reason": "not selected"},  # #2230
    ]
    # The pins the Market bundle already had are unchanged; the new pack is additive.
    assert {
        "pack_id": "economic-statistics-and-filings",
        "version": "1.1.0",
        "range": "^1.1.0",
    } in plan["source_packs"]
    off = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert not {f"noesis-knowledge-engine.{t}" for t in BAFIN_TOOLS} & set(off.tools)


def test_selecting_the_feature_binds_its_provider_and_exposes_its_tools():
    plan = plan_for([("market", ["bafin-notices"])])
    assert (
        plan["features"]["market"] == ["bafin-notices"]
        and bound(plan, "market") == FEATURE_PROVIDERS
    )
    assert plan["omissions"] == [
        {"pack": "market", "feature": "insurance", "reason": "not selected"}  # #2230
    ]
    assert {
        "pack_id": "bafin-capital-market-notices",
        "version": "1.0.0",
        "range": "^1.0.0",
    } in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert {
        "noesis-knowledge-engine.bafin_holders_as_of",
        "noesis-knowledge-engine.bafin_notice_dossier",
    } <= set(view.tools)


def test_corporate_ownership_composes_the_notices_into_its_graph_without_a_bundle_cycle():
    manifest = json.loads(
        (ROOT / "packs/corporate-ownership/manifest.json").read_text()
    )
    feature = next(
        f for f in manifest["optional_features"] if f["id"] == "bafin-voting-rights"
    )
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == {
        "market.bafin-notices",
        "ownership.records",
        "ownership.identity",
        "ownership.graph",
    }
    assert validate_composition_manifest(adapt_all()["corporate-ownership"]) == []
    plan = plan_for([("corporate-ownership", ["bafin-voting-rights"])])
    assert "market.bafin" in bound(plan, "corporate-ownership")
    both = plan_for(
        [
            ("market", ["bafin-notices"]),
            ("corporate-ownership", ["bafin-voting-rights"]),
        ]
    )
    assert both["features"] == {
        "corporate-ownership": ["bafin-voting-rights"],
        "market": ["bafin-notices"],
    }
    default = plan_for([("corporate-ownership", None)])
    assert "market.bafin" not in bound(default, "corporate-ownership")


def test_a_missing_provider_is_a_visible_omission_not_a_failure():
    descriptors = [
        d for d in provider_descriptors() if d["id"] != "platform.subscriptions"
    ]
    plan = plan_for([("market", ["bafin-notices"])], descriptors)
    assert plan["features"]["market"] == []
    omission = next(o for o in plan["omissions"] if o["feature"] == "bafin-notices")
    assert (
        omission["capability"] == "platform.subscriptions"
        and "missing_contract" in omission["reason"]
    )


def test_v1_pack_manifest_is_unchanged_and_no_new_pack_is_added():
    text = (ROOT / "packs/market/pack.json").read_text()
    assert "bafin" not in text.lower()
    assert not list(ROOT.glob("packs/*bafin*"))


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select(
        "market", bundles["market"]["version"], features=["bafin-notices"]
    )
    assert coordinator.activate("market-bafin-on")["status"] == "published"
    assert feature_enabled(conn) is True
    coordinator.select("market", bundles["market"]["version"], features=[])
    coordinator.activate("market-bafin-off")
    assert feature_enabled(conn) is False
