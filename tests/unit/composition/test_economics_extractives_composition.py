"""The Economics bundle's optional extractives features and the ``economics.extractives`` provider (#2709)."""

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
from src.kb.extractives_records import feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "economic-extractives", "version": "1.0.0", "range": "^1.0.0"}
FEATURES = ("extractives-eiti", "extractives-usgs", "extractives-bgs")


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
    result = resolve([root], list(bundles.values()), descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "economics" in b["consumers"]}


def test_descriptor_declares_read_only_operations_stores_probe_and_its_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "economics.extractives")
    assert validate_provider_descriptor(descriptor) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-extractives-record", "version": "1.0.0"}
    constraints = capability["semantic_constraints"]
    assert "no price forecasts" in constraints["exclusions"] and "never blended" in constraints["sources"]
    assert "never stored" in constraints["minimisation"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    assert {"company-payments", "country-payments", "production-reserves", "evidence-bundle"} <= {
        o["id"] for o in descriptor["operations"]}
    assert descriptor["readiness_probes"] == [{"id": "releases", "kind": "table-exists",
                                               "target": "extractives_releases"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "economics.extractives"
              for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("extractives_") for s in descriptor["stores"] for t in s["tables"])


def test_eiti_usgs_and_bgs_are_separate_optional_features_off_by_default_in_the_existing_bundle():
    composition = json.loads((ROOT / "packs/economics/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    for name in FEATURES:
        assert features[name]["default"] is False
        required = {r["capability"] for r in features[name]["requires"]}
        assert {"economics.extractives", "platform.subscriptions", "platform.source-acquisition"} <= required
        # Ownership, trade and Energy links degrade gracefully: none of their providers is required.
        assert not {c for c in required if c.split(".")[0] in {"ownership", "energy", "market"}}
        assert "provider_absent" in features[name]["description"]
    assert PACK in composition["contributes"]["source_packs"]
    assert [p["pack_id"] for p in composition["contributes"]["source_packs"]].count(
        "economic-statistics-and-filings") == 1
    assert validate_composition_manifest(adapt_all()["economics"]) == []
    assert not (ROOT / "packs/extractives").exists()  # no new pack
    pack = json.loads((ROOT / "packs/economics/pack.json").read_text())
    assert {"own reserve or production estimates", "corruption or governance risk scoring",
            "commodity price forecasts"} <= set(pack["exclusions"])


@pytest.mark.parametrize("selection", [[], ["extractives-eiti"], ["extractives-usgs"], ["extractives-bgs"],
                                       ["extractives-usgs", "trade-comtrade"]])
def test_selection_binds_the_provider_only_when_an_extractives_feature_is_selected(selection):
    plan = economics_plan(selection)
    selected = bool(set(selection) & set(FEATURES))
    assert ("economics.extractives" in bound(plan)) == selected
    if selected:
        assert PACK in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.query_extractives_production" in view.tools) == selected


def test_readiness_and_feature_enablement_follow_the_active_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] == [] and report["stores_ready"] is False
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("economics", bundles["economics"]["version"], features=["extractives-usgs"])
    assert coordinator.activate("economics-extractives-on")["status"] == "published"
    assert feature_enabled(conn) is True and feature_enabled(conn, "extractives-usgs") is True
    assert feature_enabled(conn, "extractives-eiti") is False
    assert readiness(conn)["providers"]["usgs-mcs"]["feature_selected"] is True
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `extractives-eiti`" in doc and "`noesis-extractives-record-v1`" in doc


def test_taxonomy_classifies_the_provider_and_the_gap_row_is_removed():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    entry = taxonomy["providers"]["economics.extractives"]
    assert entry == {"subdomains": ["extractives-natural-resources"],
                     "shapes": ["statistical-series", "registry-records"], "themes": ["climate"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `extractives-natural-resources` |" not in program
