"""The Economics bundle's optional ``demographics`` feature and the ``economics.demographics`` provider (#2007)."""

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
from src.kb.demographics import feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "economics.core",
    "economics.demographics",
    "legal.core",
    "political.core",
    "geospatial.core",
    "platform.subscriptions",
    "platform.source-runtime",
}
PACK = {
    "pack_id": "economic-statistics-and-filings",
    "version": "1.3.0",
    "range": "^1.3.0",
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


def economics_plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "economics", "version": bundles["economics"]["version"]}
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
    return {b["provider"] for b in plan["bindings"] if "economics" in b["consumers"]}


def test_descriptor_declares_capability_read_only_operations_stores_probe_and_source_pack():
    descriptor = next(
        d for d in provider_descriptors() if d["id"] == "economics.demographics"
    )
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {
        "name": "src.kb.demographics",
        "version": "1.0.0",
        "server": "noesis-knowledge-engine",
    }
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {
        "name": "noesis-demographic-series",
        "version": "1.0.0",
    }
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    assert all(
        "knowledge:demographics:read" in o["required_scopes"]
        for o in descriptor["operations"]
    )
    assert descriptor["readiness_probes"] == [
        {"id": "vintages", "kind": "table-exists", "target": "demographic_vintages"}
    ]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "economics.demographics"
        for s in d["stores"]
    }
    assert not owned & others
    assert all(
        t.startswith("demographic_") for s in descriptor["stores"] for t in s["tables"]
    )


def test_the_feature_and_the_migration_profile_are_declared_off_by_default():
    composition = json.loads((ROOT / "packs/economics/composition.json").read_text())
    feature = next(
        f for f in composition["optional_features"] if f["id"] == "demographics"
    )
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == {
        "economics.demographics",
        "economics.knowledge",
        "legal.works",
        "political.knowledge",
        "geospatial.feature-query",
        "geospatial.place-resolution",
        "platform.subscriptions",
        "platform.source-acquisition",
    }
    profile = next(
        p
        for p in composition["contributes"]["profiles"]
        if p["id"] == "economics.migration"
    )
    assert {
        "stock",
        "flow",
        "citizenship",
        "country of birth",
        "application",
        "decision",
    } <= set(profile["vocabulary"])
    defaults = profile["workflow_defaults"]
    assert defaults["boundary_collection"] == "alkis_bezirke:bezirksgrenzen"
    assert (
        "default_geography_level" in defaults
        and "historical_vintage_unavailable" in defaults["as_of"]
    )
    assert validate_composition_manifest(adapt_all()["economics"]) == []


@pytest.mark.parametrize(
    "selection",
    [[], ["public-finance"], ["demographics"], ["demographics", "public-finance"]],
)
def test_each_selection_resolves_independently_and_together(selection):
    plan = economics_plan(selection)
    assert sorted(plan["features"]["economics"]) == sorted(selection)
    assert ("economics.demographics" in bound(plan)) == ("demographics" in selection)
    assert ("economics.public-finance" in bound(plan)) == (
        "public-finance" in selection
    )
    if not selection:
        assert bound(plan) == {"economics.core"}
    if selection == ["demographics"]:
        assert bound(plan) == FEATURE_PROVIDERS
        # The bundle now pins 1.7.0 (the extractives sources, #2653); this provider still declares ^1.3.0.
        assert {**PACK, "version": "1.7.0", "range": "^1.7.0"} in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.query_demographic_boundary" in view.tools) == (
        "demographics" in selection
    )
    assert "noesis-kb.kb_economic" in view.tools


def test_a_missing_geospatial_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "geospatial.core"]
    plan = economics_plan(["demographics"], descriptors)
    assert "economics.demographics" not in bound(plan)
    omission = next(o for o in plan["omissions"] if o["feature"] == "demographics")
    assert (
        omission["capability"].startswith("geospatial.")
        and "missing_contract" in omission["reason"]
    )


def test_readiness_reports_each_source_decision_and_no_new_pack_or_flag():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    assert report["providers"]["bamf"]["access_decision"] == "not-implemented"
    assert {
        p["access_decision"] for k, p in report["providers"].items() if k != "bamf"
    } == {"unverified-live"}
    assert not list(ROOT.glob("packs/*demograph*")) and not list(
        ROOT.glob("config/source_packs/*demograph*")
    )
    from tools.knowledge_engine_mcp import demographics

    assert not [
        t
        for t in demographics.DEMOGRAPHIC_TOOLS
        if t.startswith("set_") and t.endswith("_enabled")
    ]
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert (
        "optional `demographics` feature" in doc
        and "`noesis-demographic-series-v1`" in doc
    )


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select(
        "economics", bundles["economics"]["version"], features=["demographics"]
    )
    assert coordinator.activate("economics-demographics-on")["status"] == "published"
    assert feature_enabled(conn) is True
    coordinator.select(
        "economics",
        bundles["economics"]["version"],
        features=["demographics", "public-finance"],
    )
    coordinator.activate("economics-both-on")
    assert feature_enabled(conn) is True
    coordinator.select("economics", bundles["economics"]["version"], features=[])
    coordinator.activate("economics-demographics-off")
    assert feature_enabled(conn) is False
