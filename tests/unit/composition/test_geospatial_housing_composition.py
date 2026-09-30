"""The Geospatial bundle's optional ``housing`` feature and the ``geospatial.housing`` provider (#2015)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    satisfies,
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.housing import feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "geospatial.core",
    "geospatial.housing",
    "legal.core",
    "economics.core",
    "political.core",
    "news.core",
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


def plan_for(pack, features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": pack, "version": bundles[pack]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve(
        [root],
        list(bundles.values()),
        descriptors if descriptors is not None else provider_descriptors(),
    )
    assert result.ok, result.failure
    return result.plan


def bound(plan, consumer="geospatial"):
    return {b["provider"] for b in plan["bindings"] if consumer in b["consumers"]}


def test_descriptor_declares_both_capabilities_operations_stores_probe_and_the_source_pack():
    descriptor = next(
        d for d in provider_descriptors() if d["id"] == "geospatial.housing"
    )
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"]["name"] == "src.kb.housing"
    assert {c["id"] for c in descriptor["capabilities"]} == {
        "geospatial.housing-records",
        "geospatial.housing-dossiers",
    }
    assert descriptor["source_packs"] == [
        {"pack_id": "geospatial-berlin", "version": "1.3.0", "range": "^1.3.0"}
    ]
    manifest = json.loads(
        (
            ROOT / "packs/geospatial/source_packs/geospatial-berlin-1.3.0.json"
        ).read_text()
    )
    assert satisfies(manifest["version"], descriptor["source_packs"][0]["range"])
    assert {s["source_id"] for s in manifest["sources"]} >= {
        "berlin-boris-bodenrichtwerte",
        "berlin-bebauungsplaene",
        "berlin-wohnlagen",
        "berlin-mietspiegel",
        "statistik-bb-bautaetigkeit",
        "destatis-genesis-bautaetigkeit",
    }
    assert all(
        "knowledge:housing:read" in o["required_scopes"]
        for o in descriptor["operations"]
    )
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "geospatial.housing"
        for s in d["stores"]
    }
    assert not owned & others
    assert all(
        t.startswith("housing_") for s in descriptor["stores"] for t in s["tables"]
    )
    assert descriptor["readiness_probes"] == [
        {
            "id": "records",
            "kind": "table-exists",
            "target": "housing_land_value_revisions",
        }
    ]


def test_the_features_and_the_dossier_profile_are_declared_off_by_default():
    composition = json.loads((ROOT / "packs/geospatial/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert {f: spec["default"] for f, spec in features.items()} == {
        "housing": False,
        "housing-transit-context": False,
        # the critical infrastructure features (#2223), also default off
        "infrastructure": False,
        "infrastructure-ownership": False,
        "infrastructure-citation-links": False,
    }
    assert {r["capability"] for r in features["housing"]["requires"]} == {
        "geospatial.housing-records",
        "geospatial.housing-dossiers",
        "geospatial.feature-query",
        "geospatial.place-resolution",
        "legal.works",
        "economics.knowledge",
        "political.knowledge",
        "news.articles",
        "platform.subscriptions",
        "platform.source-acquisition",
    }
    assert {
        r["capability"] for r in features["housing-transit-context"]["requires"]
    } == {"geospatial.housing-dossiers", "geospatial.transit-schedules"}
    (profile,) = composition["contributes"]["profiles"]
    assert profile["id"] == "geospatial.housing-dossier"
    assert (
        profile["workflow_defaults"]["boundary_collection"]
        == "alkis_bezirke:bezirksgrenzen"
    )
    assert {"Bodenrichtwert", "Stichtag", "Mietspiegel", "Wohnlage"} <= set(
        profile["vocabulary"]
    )
    assert composition["contributes"]["source_packs"] == [
        {"pack_id": "geospatial-berlin", "version": "1.1.0", "range": "^1.1.0"}
    ]
    assert validate_composition_manifest(adapt_all()["geospatial"]) == []


@pytest.mark.parametrize(
    "selection", [[], ["housing"], ["housing", "housing-transit-context"]]
)
def test_each_selection_resolves_and_binds_only_what_it_needs(selection):
    plan = plan_for("geospatial", selection)
    assert sorted(plan["features"]["geospatial"]) == sorted(selection)
    assert ("geospatial.housing" in bound(plan)) == ("housing" in selection)
    if not selection:
        assert bound(plan) <= {"geospatial.core", "geospatial.transit"}
    if selection == ["housing"]:
        assert bound(plan) == FEATURE_PROVIDERS
    if "housing-transit-context" in selection:
        assert bound(plan) == FEATURE_PROVIDERS | {"geospatial.transit"}
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.housing_dossier" in view.tools) == (
        "housing" in selection
    )


def test_osint_and_science_keep_their_geospatial_bindings():
    for pack in ("osint", "science"):
        plan = plan_for(pack)
        providers = {b["provider"] for b in plan["bindings"] if pack in b["consumers"]}
        assert "geospatial.housing" not in providers
        assert not plan["features"].get("geospatial")


def test_a_missing_legal_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "legal.core"]
    plan = plan_for("geospatial", ["housing"], descriptors)
    assert "geospatial.housing" not in bound(plan)
    omission = next(o for o in plan["omissions"] if o["feature"] == "housing")
    assert (
        omission["capability"] == "legal.works"
        and "missing_contract" in omission["reason"]
    )


def test_readiness_reports_each_source_decision_and_no_new_pack_or_flag():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    assert report["providers"]["boris-berlin"]["access_decision"] == "not-implemented"
    assert not list(ROOT.glob("packs/*housing*")) and not list(
        ROOT.glob("config/source_packs/*housing*")
    )
    from tools.knowledge_engine_mcp import housing

    assert not [
        t
        for t in housing.HOUSING_TOOLS
        if t.startswith("set_") and t.endswith("_enabled")
    ]
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `housing` feature" in doc and "`noesis-housing-record-v1`" in doc


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select(
        "geospatial", bundles["geospatial"]["version"], features=["housing"]
    )
    assert coordinator.activate("geospatial-housing-on")["status"] == "published"
    assert feature_enabled(conn) is True
    coordinator.select("geospatial", bundles["geospatial"]["version"], features=[])
    coordinator.activate("geospatial-housing-off")
    assert feature_enabled(conn) is False
