"""The Economics bundle's optional ``tourism-occupancy`` and ``tourism-capacity`` features (one per source) and the
``economics.tourism`` provider (#2739)."""

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
from src.ingestion.source_packs import validate_source_pack
from src.kb.tourism_records import (
    FEATURES,
    enabled_providers,
    feature_enabled,
    readiness,
)
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "economics.core",
    "economics.tourism",
    "platform.subscriptions",
    "platform.source-runtime",
}
PACK = {"pack_id": "economic-statistics-and-filings", "version": "1.8.0", "range": "^1.8.0"}


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
    descriptor = next(d for d in provider_descriptors() if d["id"] == "economics.tourism")
    assert validate_provider_descriptor(descriptor) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-tourism-statistics-record", "version": "2.0.0"}
    constraints = capability["semantic_constraints"]
    assert {"series", "vintages", "flags", "definitions", "comparability", "identity", "links", "sources",
            "minimisation", "exclusions"} <= set(constraints)
    assert "no nowcasting" in constraints["exclusions"] and "no blending of Eurostat and UN Tourism" in constraints[
        "exclusions"]
    assert "never computed from months" in constraints["series"]
    assert "provider_absent" in constraints["links"] and "not-implemented" in constraints["sources"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    tools = {o["tool"].split(".", 1)[1] for o in descriptor["operations"]}
    assert {"tourism_indicator_for_place", "tourism_series_history", "export_tourism_evidence_bundle"} <= tools
    assert descriptor["readiness_probes"] == [{"id": "vintages", "kind": "table-exists",
                                               "target": "tourism_vintages"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "economics.tourism"
              for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("tourism_") for s in descriptor["stores"] for t in s["tables"])


def test_tourism_features_are_optional_per_source_features_of_the_existing_economics_pack():
    composition = json.loads((ROOT / "packs/economics/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(FEATURES) == {"tourism-occupancy", "tourism-capacity"}
    for name, source in FEATURES.items():
        feature = features[name]
        assert feature["default"] is False and source in feature["description"]
        assert {r["capability"] for r in feature["requires"]} == {
            "economics.tourism", "economics.knowledge", "platform.subscriptions", "platform.source-acquisition"}
        # Geospatial and Labour links degrade gracefully: neither provider is required.
        assert "provider_absent" in feature["description"] and "No nowcasting" in feature["description"]
    profile = next(p for p in composition["contributes"]["profiles"] if p["id"] == "economics.tourism")
    assert {"nights spent", "arrivals", "residence of guest", "accommodation type", "NUTS version",
            "establishment threshold", "vintage", "confidential"} <= set(profile["vocabulary"])
    assert PACK in composition["contributes"]["source_packs"]
    assert [p["pack_id"] for p in composition["contributes"]["source_packs"]].count(
        "economic-statistics-and-filings") == 1
    installed = validate_source_pack(json.loads((ROOT / "config/source_packs/economic.json").read_text()))
    assert installed["version"] == PACK["version"]
    assert validate_composition_manifest(adapt_all()["economics"]) == []
    pack = json.loads((ROOT / "packs/economics/pack.json").read_text())
    assert {"tourism nowcasts or forecasts", "filled tourism months or regions",
            "own occupancy rates, averages, per-capita or per-bed tourism figures",
            "blended Eurostat and UN Tourism statistics", "annual tourism totals computed from months"} <= set(
        pack["exclusions"])
    assert pack["schema_versions"]["tourism-statistics-record"] == "2.0.0"
    assert {"tourism", "nights spent"} <= set(pack["planner_keywords"]["claims"])
    assert {"tourism_series", "nuts_correspondence_link"} <= set(pack["ontology_extensions"]["object_types"])
    assert not list(ROOT.glob("packs/*tourism*"))  # no new pack


@pytest.mark.parametrize("selection", [[], ["tourism-occupancy"], ["tourism-capacity"],
                                       ["tourism-occupancy", "tourism-capacity", "labour-statistics"],
                                       ["tourism-capacity", "business-statistics"]])
def test_each_selection_resolves_independently_and_together(selection):
    plan = economics_plan(selection)
    tourism = bool(set(selection) & set(FEATURES))
    assert ("economics.tourism" in bound(plan)) == tourism
    if selection in (["tourism-occupancy"], ["tourism-capacity"]):
        assert bound(plan) == FEATURE_PROVIDERS  # Geospatial and Labour are not required
        assert PACK in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.tourism_indicator_for_place" in view.tools) == tourism
    assert "noesis-kb.kb_economic" in view.tools


def test_links_degrade_to_provider_absent_without_geospatial_and_labour():
    from tests.unit import tourism_harness as h

    conn = h.connection()
    h.load_all(conn)
    from src.kb.tourism_links import TourismLinks

    links = TourismLinks(conn)
    links.link_boundaries(h.NS, principal_id="svc", scopes=h.SCOPES)
    links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert {link["state"] for link in links.links(h.NS, scopes=h.READ_ONLY)} == {"provider_absent"}


def test_readiness_and_feature_enablement_follow_the_composition_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live", "not-implemented"}
    # Before composition manages the bundle every source answers; afterwards only the selected ones.
    assert enabled_providers(duckdb.connect(":memory:")) == set(FEATURES.values())
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("economics", bundles["economics"]["version"], features=["tourism-capacity"])
    assert coordinator.activate("economics-tourism-on")["status"] == "published"
    assert feature_enabled(conn) is True and feature_enabled(conn, "tourism-occupancy") is False
    assert enabled_providers(conn) == {"eurostat-tourism-capacity"}
    status = readiness(conn)
    assert status["features_selected"] == ["tourism-capacity"]
    assert status["providers"]["eurostat-tourism-capacity"]["feature_selected"] is True
    assert status["providers"]["eurostat-tourism-occupancy"]["feature_selected"] is False
    coordinator.select("economics", bundles["economics"]["version"], features=[])
    coordinator.activate("economics-tourism-off")
    assert feature_enabled(conn) is False and enabled_providers(conn) == set()
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `tourism-occupancy` and `tourism-capacity` features" in doc
    assert "`noesis-tourism-statistics-record-v2`" in doc


def test_taxonomy_classifies_the_provider_and_the_gap_row_is_removed():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["economics.tourism"] == {"subdomains": ["tourism-hospitality"],
                                                          "shapes": ["statistical-series"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `tourism-hospitality` |" not in program
    assert "68 are covered and 21 are gaps" in program
    assert "covered offline (not live)" in program
    readme = (ROOT / "README.md").read_text()
    assert "Today 68 are covered by fixture-tested providers. The other 21 are" in readme
