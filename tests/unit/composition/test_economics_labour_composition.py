"""The Economics bundle's optional ``labour-statistics`` feature and the ``economics.labour`` provider (#2487)."""

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
from src.kb.labour_statistics import feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "economics.core",
    "economics.labour",
    "geospatial.core",
    "platform.subscriptions",
    "platform.source-runtime",
}
PACK = {"pack_id": "economic-statistics-and-filings", "version": "1.6.0", "range": "^1.6.0"}


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
    descriptor = next(d for d in provider_descriptors() if d["id"] == "economics.labour")
    assert validate_provider_descriptor(descriptor) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-labour-statistics-record", "version": "1.0.0"}
    constraints = capability["semantic_constraints"]
    assert {"definitions", "vintages", "comparability", "exclusions"} <= set(constraints)
    assert "nowcasting" in constraints["exclusions"] and "forecasts" in constraints["exclusions"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    tools = {o["tool"].split(".", 1)[1] for o in descriptor["operations"]}
    assert {"labour_indicators_for_place", "labour_series_history", "labour_comparability_notes"} <= tools
    assert descriptor["readiness_probes"] == [{"id": "vintages", "kind": "table-exists", "target": "labour_vintages"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "economics.labour" for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("labour_") for s in descriptor["stores"] for t in s["tables"])


def test_labour_is_an_optional_feature_of_the_existing_economics_pack():
    composition = json.loads((ROOT / "packs/economics/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    feature = features["labour-statistics"]
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == {
        "economics.labour", "economics.knowledge", "geospatial.place-resolution", "platform.subscriptions",
        "platform.source-acquisition"}
    assert "nowcasting" in feature["description"].lower() or "No nowcasting" in feature["description"]
    profile = next(p for p in composition["contributes"]["profiles"] if p["id"] == "economics.labour")
    assert {"definition basis", "seasonal adjustment", "vintage", "benchmark revision"} <= set(profile["vocabulary"])
    assert validate_composition_manifest(adapt_all()["economics"]) == []
    pack = json.loads((ROOT / "packs/economics/pack.json").read_text())
    assert "labour-market nowcasts or forecasts" in pack["exclusions"]
    assert pack["schema_versions"]["labour-statistics-record"] == "1.0.0"
    assert not list(ROOT.glob("packs/*labour*"))


@pytest.mark.parametrize("selection", [[], ["labour-statistics"], ["labour-statistics", "demographics"],
                                       ["labour-statistics", "trade-comext"]])
def test_each_selection_resolves_independently_and_together(selection):
    plan = economics_plan(selection)
    labour = "labour-statistics" in selection
    assert ("economics.labour" in bound(plan)) == labour
    if selection == ["labour-statistics"]:
        assert bound(plan) == FEATURE_PROVIDERS
        assert PACK in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.labour_indicators_for_place" in view.tools) == labour
    assert "noesis-kb.kb_economic" in view.tools


def test_readiness_and_feature_enablement_follow_the_composition_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("economics", bundles["economics"]["version"], features=["labour-statistics"])
    assert coordinator.activate("economics-labour-on")["status"] == "published"
    assert feature_enabled(conn) is True
    coordinator.select("economics", bundles["economics"]["version"], features=[])
    coordinator.activate("economics-labour-off")
    assert feature_enabled(conn) is False
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `labour-statistics` feature" in doc and "`noesis-labour-statistics-record-v1`" in doc
