"""The Economics bundle's optional ``trade-comtrade`` and ``trade-comext`` features and the ``economics.trade``
provider (#2554)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.trade_flows import feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "economics.core",
    "economics.trade",
    "geospatial.core",
    "platform.subscriptions",
    "platform.source-runtime",
}
PACK = {"pack_id": "economic-statistics-and-filings", "version": "1.5.0", "range": "^1.5.0"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def economics_plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "economics", "version": bundles["economics"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()),
                     descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "economics" in b["consumers"]}


def test_descriptor_declares_capability_read_only_operations_stores_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "economics.trade")
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {"name": "src.kb.trade_flows", "version": "1.0.0",
                                            "server": "noesis-knowledge-engine"}
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-trade-flow-record", "version": "1.0.0"}
    assert "estimation" in capability["semantic_constraints"]["exclusions"]
    assert "evasion" in capability["semantic_constraints"]["exclusions"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    assert all("knowledge:trade:read" in o["required_scopes"] for o in descriptor["operations"])
    assert descriptor["readiness_probes"] == [{"id": "vintages", "kind": "table-exists", "target": "trade_vintages"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "economics.trade" for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("trade_") for s in descriptor["stores"] for t in s["tables"])
    # The existing Economics providers are unchanged.
    for name in ("economics.core", "economics.demographics", "economics.public-finance"):
        assert next(d for d in provider_descriptors() if d["id"] == name)["version"] == "1.0.0"


def test_comtrade_and_comext_are_separate_optional_features_off_by_default_without_sanctions_or_ownership():
    composition = json.loads((ROOT / "packs/economics/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    for name in ("trade-comtrade", "trade-comext"):
        assert features[name]["default"] is False
        required = {r["capability"] for r in features[name]["requires"]}
        assert required == {"economics.trade", "economics.knowledge", "geospatial.place-resolution",
                            "platform.subscriptions", "platform.source-acquisition"}
        assert not {c for c in required if c.startswith(("legal.", "ownership."))}
    profile = next(p for p in composition["contributes"]["profiles"] if p["id"] == "economics.trade-flows")
    assert {"reporter figure", "mirror figure", "asymmetry", "CIF", "FOB"} <= set(profile["vocabulary"])
    assert "never reconciled" in profile["workflow_defaults"]["mirror"]
    assert validate_composition_manifest(adapt_all()["economics"]) == []
    pack = json.loads((ROOT / "packs/economics/pack.json").read_text())
    assert "reconciled reporter and mirror figures" in pack["exclusions"]


@pytest.mark.parametrize(
    "selection",
    [[], ["trade-comtrade"], ["trade-comext"], ["trade-comext", "trade-comtrade"], ["demographics", "trade-comext"]],
)
def test_each_selection_resolves_independently_and_together(selection):
    plan = economics_plan(selection)
    assert sorted(plan["features"]["economics"]) == sorted(selection)
    trade = any(f.startswith("trade-") for f in selection)
    assert ("economics.trade" in bound(plan)) == trade
    if not selection:
        assert bound(plan) == {"economics.core"}
    if selection in (["trade-comtrade"], ["trade-comext"]):
        assert bound(plan) == FEATURE_PROVIDERS
        # The bundle now pins 1.7.0 (the extractives sources, #2653); this provider still declares ^1.5.0.
        assert {**PACK, "version": "1.7.0", "range": "^1.7.0"} in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.query_trade_flows" in view.tools) == trade
    assert "noesis-kb.kb_economic" in view.tools


def test_a_missing_geospatial_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "geospatial.core"]
    plan = economics_plan(["trade-comtrade"], descriptors)
    assert "economics.trade" not in bound(plan)
    omission = next(o for o in plan["omissions"] if o["feature"] == "trade-comtrade")
    assert omission["capability"].startswith("geospatial.") and "missing_contract" in omission["reason"]


def test_readiness_reports_each_source_decision_and_no_new_pack_or_flag():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    assert report["features"] == {"trade-comtrade": False, "trade-comext": False}
    assert report["providers"]["unsd-classifications"]["access_decision"] == "operator-import"
    assert {p["live_verification"] for k, p in report["providers"].items() if k != "unsd-classifications"} == {
        "unverified-live"}
    assert not list(ROOT.glob("packs/*trade*")) and not list(ROOT.glob("config/source_packs/*trade*"))
    from tools.knowledge_engine_mcp import trade

    assert not [t for t in trade.TRADE_TOOLS if t.startswith("set_") and t.endswith("_enabled")]
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `trade-comtrade` and `trade-comext` features" in doc and "`noesis-trade-flow-record-v1`" in doc


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("economics", bundles["economics"]["version"], features=["trade-comext"])
    assert coordinator.activate("economics-trade-comext-on")["status"] == "published"
    assert feature_enabled(conn) is True and feature_enabled(conn, "trade-comext") is True
    assert feature_enabled(conn, "trade-comtrade") is False
    coordinator.select("economics", bundles["economics"]["version"], features=[])
    coordinator.activate("economics-trade-off")
    assert feature_enabled(conn) is False
