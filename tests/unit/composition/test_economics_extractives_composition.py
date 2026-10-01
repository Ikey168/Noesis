"""The Economics bundle's optional ``extractives-eiti``, ``extractives-usgs`` and ``extractives-bgs`` features and the
``economics.extractives`` provider (#2709)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.extractives_records import feature_enabled
from src.kb.extractives_store import readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURES = ("extractives-eiti", "extractives-usgs", "extractives-bgs")
FEATURE_PROVIDERS = {"economics.core", "economics.extractives", "platform.subscriptions", "platform.source-runtime"}
PACK = {"pack_id": "economic-statistics-and-filings", "version": "1.7.0", "range": "^1.7.0"}


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
    descriptor = next(d for d in provider_descriptors() if d["id"] == "economics.extractives")
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {"name": "src.kb.extractives_store", "version": "1.0.0",
                                            "server": "noesis-knowledge-engine"}
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-extractives-record", "version": "1.0.0"}
    constraints = capability["semantic_constraints"]
    assert {"reports", "payments", "series", "vintages", "identity", "links", "minimisation",
            "exclusions"} <= set(constraints)
    for phrase in ("reconciliation", "reserve estimates", "risk scoring", "price forecasts"):
        assert phrase in constraints["exclusions"]
    assert "never converted" in constraints["payments"] and "never blended" in constraints["series"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    assert all("knowledge:extractives:read" in o["required_scopes"] for o in descriptor["operations"])
    assert descriptor["readiness_probes"] == [{"id": "vintages", "kind": "table-exists", "target": "ex_vintages"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "economics.extractives"
              for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("ex_") for s in descriptor["stores"] for t in s["tables"])
    # The existing Economics providers are unchanged.
    for name in ("economics.core", "economics.demographics", "economics.public-finance", "economics.trade",
                 "economics.labour"):
        assert next(d for d in provider_descriptors() if d["id"] == name)["version"] == "1.0.0"


def test_eiti_usgs_and_bgs_are_separate_optional_features_of_the_existing_pack():
    composition = json.loads((ROOT / "packs/economics/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    for name in FEATURES:
        assert features[name]["default"] is False
        required = {r["capability"] for r in features[name]["requires"]}
        assert required == {"economics.extractives", "economics.knowledge", "platform.subscriptions",
                            "platform.source-acquisition"}
        # Ownership, trade, energy and infrastructure are consumed when present and degrade when absent.
        assert not {c for c in required if c.startswith(("ownership.", "energy.", "geospatial."))}
    profile = next(p for p in composition["contributes"]["profiles"] if p["id"] == "economics.extractives")
    assert {"EITI report", "revenue stream", "discrepancy", "withheld (W)", "vintage"} <= set(profile["vocabulary"])
    assert "never converted or summed" in profile["workflow_defaults"]["payments"]
    assert validate_composition_manifest(adapt_all()["economics"]) == []
    pack = json.loads((ROOT / "packs/economics/pack.json").read_text())
    assert {"own reserve estimates", "corruption or governance risk scoring", "commodity price forecasts"} <= set(
        pack["exclusions"])
    assert pack["schema_versions"]["extractives-record"] == "1.0.0"
    assert not list(ROOT.glob("packs/*extractive*"))
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    entry = taxonomy["providers"]["economics.extractives"]
    assert entry == {"subdomains": ["extractives-natural-resources"],
                     "shapes": ["statistical-series", "registry-records"], "themes": ["climate"]}
    assert "`extractives-natural-resources`" not in (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()


@pytest.mark.parametrize("selection", [[], ["extractives-eiti"], ["extractives-usgs"], ["extractives-bgs"],
                                       ["extractives-bgs", "extractives-eiti", "extractives-usgs"],
                                       ["extractives-usgs", "trade-comext"]])
def test_each_selection_resolves_independently_and_together(selection):
    plan = economics_plan(selection)
    assert sorted(plan["features"]["economics"]) == sorted(selection)
    extractives = any(f.startswith("extractives-") for f in selection)
    assert ("economics.extractives" in bound(plan)) == extractives
    if not selection:
        assert bound(plan) == {"economics.core"}
    if len(selection) == 1:
        assert bound(plan) == FEATURE_PROVIDERS
        assert PACK in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.extractive_payments_for_company" in view.tools) == extractives
    assert ("noesis-knowledge-engine.commodity_production_side_by_side" in view.tools) == extractives
    assert "noesis-kb.kb_economic" in view.tools


def test_a_missing_subscriptions_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "platform.subscriptions"]
    plan = economics_plan(["extractives-eiti"], descriptors)
    assert "economics.extractives" not in bound(plan)
    omission = next(o for o in plan["omissions"] if o["feature"] == "extractives-eiti")
    assert omission["capability"].startswith("platform.") and "missing_contract" in omission["reason"]


def test_readiness_and_feature_enablement_follow_the_composition_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    assert report["features"] == {f: False for f in FEATURES}
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("economics", bundles["economics"]["version"], features=["extractives-usgs"])
    assert coordinator.activate("economics-extractives-usgs-on")["status"] == "published"
    assert feature_enabled(conn) is True and feature_enabled(conn, "extractives-usgs") is True
    assert feature_enabled(conn, "extractives-eiti") is False
    coordinator.select("economics", bundles["economics"]["version"], features=[])
    coordinator.activate("economics-extractives-off")
    assert feature_enabled(conn) is False
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `extractives-eiti`, `extractives-usgs` and `extractives-bgs` features" in doc
    assert "`noesis-extractives-record-v1`" in doc
