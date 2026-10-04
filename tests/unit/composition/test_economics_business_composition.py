"""The Economics bundle's optional ``business-statistics`` feature and the ``economics.business`` provider (#2738)."""

from __future__ import annotations

import json
import re
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
from src.ingestion.source_packs import validate_source_pack
from src.kb.business_statistics_records import feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "economics.core",
    "economics.business",
    "geospatial.core",
    "platform.subscriptions",
    "platform.source-runtime",
}
PACK = {"pack_id": "economic-statistics-and-filings", "version": "1.7.0", "range": "^1.7.0"}
# The bundle now pins 1.8.0 (the tourism sources, #2739); this provider still declares ^1.7.0, which it meets.
BUNDLE_PACK = {**PACK, "version": "1.8.0", "range": "^1.8.0"}


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
    descriptor = next(d for d in provider_descriptors() if d["id"] == "economics.business")
    assert validate_provider_descriptor(descriptor) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-business-statistics-record", "version": "2.0.0"}
    constraints = capability["semantic_constraints"]
    assert {"series", "vintages", "flags", "comparability", "identity", "links", "minimisation",
            "exclusions"} <= set(constraints)
    assert "no nowcasting" in constraints["exclusions"] and "no blending of Eurostat and Census" in constraints[
        "exclusions"]
    assert "never reconstructed" in constraints["flags"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    tools = {o["tool"].split(".", 1)[1] for o in descriptor["operations"]}
    assert {"business_indicator_for_place", "compare_business_places", "business_series_history"} <= tools
    assert descriptor["readiness_probes"] == [{"id": "vintages", "kind": "table-exists",
                                               "target": "business_vintages"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "economics.business"
              for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("business_") for s in descriptor["stores"] for t in s["tables"])


def test_business_statistics_is_an_optional_feature_of_the_existing_economics_pack():
    composition = json.loads((ROOT / "packs/economics/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    feature = features["business-statistics"]
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == {
        "economics.business", "economics.knowledge", "geospatial.place-resolution", "platform.subscriptions",
        "platform.source-acquisition"}
    # Labour and Trade links degrade gracefully: neither provider is required.
    assert "provider_absent" in feature["description"] and "No nowcasting" in feature["description"]
    profile = next(p for p in composition["contributes"]["profiles"] if p["id"] == "economics.business")
    assert {"statistical unit", "adjustment", "base year", "vintage", "noise flag", "withheld",
            "candidate link"} <= set(profile["vocabulary"])
    # The bundle pins the pack version that carries the business sources; one pin per pack.
    assert BUNDLE_PACK in composition["contributes"]["source_packs"]
    assert [p["pack_id"] for p in composition["contributes"]["source_packs"]].count(
        "economic-statistics-and-filings") == 1
    installed = validate_source_pack(json.loads((ROOT / "config/source_packs/economic.json").read_text()))
    assert installed["version"] == BUNDLE_PACK["version"]
    assert validate_composition_manifest(adapt_all()["economics"]) == []
    pack = json.loads((ROOT / "packs/economics/pack.json").read_text())
    assert {"business-indicator nowcasts or forecasts", "re-based indices or own seasonal adjustment",
            "blended Eurostat and Census business statistics",
            "reconstructed suppressed or noise-infused cells"} <= set(pack["exclusions"])
    assert pack["schema_versions"]["business-statistics-record"] == "2.0.0"
    assert not list(ROOT.glob("packs/*business*"))  # no new pack


@pytest.mark.parametrize("selection", [[], ["business-statistics"], ["business-statistics", "labour-statistics"],
                                       ["business-statistics", "trade-comext"]])
def test_each_selection_resolves_independently_and_together(selection):
    plan = economics_plan(selection)
    business = "business-statistics" in selection
    assert ("economics.business" in bound(plan)) == business
    if selection == ["business-statistics"]:
        assert bound(plan) == FEATURE_PROVIDERS
        assert BUNDLE_PACK in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.business_indicator_for_place" in view.tools) == business
    assert "noesis-kb.kb_economic" in view.tools


def test_readiness_and_feature_enablement_follow_the_composition_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("economics", bundles["economics"]["version"], features=["business-statistics"])
    assert coordinator.activate("economics-business-on")["status"] == "published"
    assert feature_enabled(conn) is True
    coordinator.select("economics", bundles["economics"]["version"], features=[])
    coordinator.activate("economics-business-off")
    assert feature_enabled(conn) is False
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `business-statistics` feature" in doc and "`noesis-business-statistics-record-v2`" in doc


def test_taxonomy_classifies_the_provider_and_the_gap_row_is_removed():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["economics.business"] == {"subdomains": ["industry-business"],
                                                           "shapes": ["statistical-series"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `industry-business` |" not in program
    covered, gaps = map(int, re.search(r"(\d+) are covered and (\d+) are gaps", " ".join(program.split())).groups())
    assert covered + gaps == 89 and covered >= 67
