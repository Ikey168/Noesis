"""The Astronomy bundle: native manifest, providers, optional features and MCP tool exposure (#2149, AS11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    satisfies,
    validate_composition_manifest,
    validate_provider_descriptor,
    validate_provider_set,
)
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.astronomy_store import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.astronomy import ASTRONOMY_TOOLS
from tools.knowledge_engine_mcp.astronomy_registration import REGISTRATION_TOOLS

ROOT = Path(__file__).resolve().parents[3]
CORE = {
    "astronomy.small-bodies",
    "astronomy.exoplanets",
    "platform.entity-identity",
    "platform.subscriptions",
    "platform.source-runtime",
    "science.literature",
    "science.paper-families",
}
LAUNCH_TOOLS = {
    "lookup_launches",
    "orbital_object_history",
    "review_astronomy_launch_site",
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


def plan_for(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "astronomy", "version": bundles["astronomy"]["version"]}
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
    return {b["provider"] for b in plan["bindings"] if "astronomy" in b["consumers"]}


def tools(plan):
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    return {
        t.split(".", 1)[1]
        for t in view.tools
        if t.startswith("noesis-knowledge-engine.")
    } & ASTRONOMY_TOOLS


def test_manifest_view_providers_and_source_pack_are_consistent():
    manifest = json.loads((ROOT / "packs/astronomy/manifest.json").read_text())
    view = json.loads((ROOT / "packs/astronomy/composition.json").read_text())
    assert validate_composition_manifest(adapt_all()["astronomy"]) == []
    assert (
        view["requires"] == manifest["requires"]
        and view["optional_features"] == manifest["optional_features"]
    )
    assert view["exclusions"] == manifest["advisory"]["exclusions"]
    assert any("orbit determination" in e for e in view["exclusions"])
    assert any("impact-risk" in e for e in view["exclusions"])
    assert any("disposition" in e for e in view["exclusions"])
    assert all(
        example["semantics"] for example in manifest["advisory"]["query_examples"]
    )
    descriptors = [
        d for d in provider_descriptors() if d["id"].startswith("astronomy.")
    ]
    assert {d["id"] for d in descriptors} == {
        "astronomy.small-bodies",
        "astronomy.exoplanets",
        "astronomy.launches",
        "astronomy.space-weather",
        "astronomy.space-object-registration",
    }
    assert all(validate_provider_descriptor(d) == [] for d in descriptors)
    assert validate_provider_set(provider_descriptors()) == []
    owners = [
        d["id"]
        for d in provider_descriptors()
        for s in d["stores"]
        if s["store"] == "src.kb.astronomy_store"
    ]
    assert owners == ["astronomy.small-bodies"]
    installed = json.loads((ROOT / "config/source_packs/astronomy.json").read_text())
    assert installed["pack_id"] == "astronomy-and-space" and satisfies(
        installed["version"], "^1.0.0"
    )
    assert {
        t.split(".", 1)[1]
        for d in descriptors
        for t in (o["tool"] for o in d["operations"])
    } == ASTRONOMY_TOOLS | REGISTRATION_TOOLS


def test_features_are_off_by_default_and_their_tools_stay_hidden():
    features = {
        f["id"]: f
        for f in json.loads((ROOT / "packs/astronomy/manifest.json").read_text())[
            "optional_features"
        ]
    }
    assert set(features) == {
        "astronomy-launches",
        "astronomy-space-weather",
        "astronomy-space-object-registration",
        "astronomy-discos",
    }
    assert not any(f["default"] for f in features.values())
    plan = plan_for()
    assert plan["features"]["astronomy"] == [] and bound(plan) == CORE
    exposed = tools(plan)
    assert "small_body_history" in exposed and "exoplanet_status_as_of" in exposed
    assert not (LAUNCH_TOOLS | {"space_weather_alerts"}) & exposed


@pytest.mark.parametrize(
    ("features", "providers", "visible"),
    [
        (
            ["astronomy-launches"],
            {"astronomy.launches", "geospatial.core"},
            LAUNCH_TOOLS,
        ),
        (
            ["astronomy-space-weather"],
            {"astronomy.space-weather"},
            {"space_weather_alerts"},
        ),
        (
            ["astronomy-launches", "astronomy-space-weather"],
            {"astronomy.launches", "geospatial.core", "astronomy.space-weather"},
            LAUNCH_TOOLS | {"space_weather_alerts"},
        ),
    ],
)
def test_each_feature_on_binds_its_provider_and_exposes_its_tools(
    features, providers, visible
):
    plan = plan_for(features)
    assert plan["features"]["astronomy"] == sorted(features)
    assert bound(plan) == CORE | providers
    assert visible <= tools(plan)
    assert {
        "pack_id": "astronomy-and-space",
        "version": "1.0.0",
        "range": "^1.0.0",
    } in plan["source_packs"]


def test_a_missing_shipped_provider_blocks_resolution_instead_of_being_substituted():
    bundles = adapt_all()
    descriptors = [
        d for d in provider_descriptors() if d["id"] != "astronomy.space-weather"
    ]
    result = resolve(
        [
            {
                "pack": "astronomy",
                "version": bundles["astronomy"]["version"],
                "features": ["astronomy-space-weather"],
            }
        ],
        list(bundles.values()),
        descriptors,
    )
    assert not result.ok and result.failure.code == "missing_provider"


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn, "astronomy-launches") is False
    coordinator.select(
        "astronomy", bundles["astronomy"]["version"], features=["astronomy-launches"]
    )
    assert coordinator.activate("astronomy-launches-on")["status"] == "published"
    assert feature_enabled(conn, "astronomy-launches") is True
    assert feature_enabled(conn, "astronomy-space-weather") is False
    coordinator.select("astronomy", bundles["astronomy"]["version"], features=[])
    coordinator.activate("astronomy-launches-off")
    assert feature_enabled(conn, "astronomy-launches") is False
