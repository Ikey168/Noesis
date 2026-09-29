"""The Weather bundle's composition, providers and optional features (WX12, #2175)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.domains.pack_format import validate_manifest
from src.kb.weather_bundle import feature_allowed, feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.weather import (
    WEATHER_READS,
    WEATHER_SCOPES,
    WEATHER_TOOLS,
)

ROOT = Path(__file__).resolve().parents[3]
BASE = {
    "weather.observations",
    "weather.forecasts",
    "weather.warnings",
    "environment.core",
    "geospatial.core",
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


def plan(features=None):
    bundles = adapt_all()
    root = {"pack": "weather", "version": bundles["weather"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(result):
    return {b["provider"] for b in result["bindings"] if "weather" in b["consumers"]}


def test_descriptors_declare_operations_with_the_tools_exact_scopes_and_one_store_owner():
    descriptors = {
        d["id"]: d for d in provider_descriptors() if d["id"].startswith("weather.")
    }
    assert set(descriptors) == {
        "weather.observations",
        "weather.forecasts",
        "weather.warnings",
        "weather.verification",
    }
    catalog = {
        t["name"]: t
        for t in json.loads(
            (ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
        )["tools"]
    }
    covered = set()
    for descriptor in descriptors.values():
        assert validate_provider_descriptor(descriptor) == []
        for operation in descriptor["operations"]:
            tool = operation["tool"].split(".", 1)[1]
            covered.add(tool)
            assert (
                operation["required_scopes"]
                == WEATHER_SCOPES[tool]
                == catalog[tool]["required_scopes"]
            )
            assert (operation["side_effect"] == "read-only") == (
                tool in WEATHER_READS
            ), tool
    assert covered == WEATHER_TOOLS
    owners = [
        d["id"]
        for d in provider_descriptors()
        for s in d["stores"]
        if s["store"] == "src.kb.weather_store"
    ]
    assert owners == ["weather.observations"]
    assert descriptors["weather.observations"]["source_packs"] == [
        {"pack_id": "weather-operational", "version": "1.0.0", "range": "^1.0.0"}
    ]


def test_the_v1_manifest_is_valid_and_states_the_tracker_exclusions():
    pack = json.loads((ROOT / "packs/weather/pack.json").read_text())
    assert validate_manifest(pack) == []
    exclusions = " ".join(pack["exclusions"])
    assert (
        "own forecasting" in exclusions
        and "advice" in exclusions
        and "Climate & Environment" in exclusions
    )
    assert {e["tool"] for e in pack["query_examples"]} <= WEATHER_TOOLS
    assert validate_composition_manifest(adapt_all()["weather"]) == []


def test_features_are_off_by_default_and_climate_environment_is_a_required_dependency():
    result = plan()
    assert result["features"]["weather"] == [] and bound(result) == BASE
    assert {
        (o["feature"], o["reason"])
        for o in result["omissions"]
        if o["pack"] == "weather"
    } == {
        ("weather-open-meteo", "not selected"),
        ("weather-verification", "not selected"),
    }
    assert "climate-environment" in {p["id"] for p in result["packs"]}
    assert {
        "pack_id": "weather-operational",
        "version": "1.0.0",
        "range": "^1.0.0",
    } in result["source_packs"]


def test_selecting_the_features_binds_verification_with_one_provider_per_capability():
    result = plan(["weather-open-meteo", "weather-verification"])
    assert sorted(result["features"]["weather"]) == [
        "weather-open-meteo",
        "weather-verification",
    ]
    assert bound(result) == BASE | {"weather.verification"}
    bindings = [b for b in result["bindings"] if "weather" in b["consumers"]]
    assert len({b["capability"] for b in bindings}) == len(bindings)
    assert not [o for o in result["omissions"] if o["pack"] == "weather"]


def test_feature_gates_follow_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn, "weather-open-meteo") is False
    assert (
        feature_allowed(conn, "weather-verification") is False
    )  # composed: the selection decides
    coordinator.select(
        "weather",
        bundles["weather"]["version"],
        features=["weather-open-meteo", "weather-verification"],
    )
    assert coordinator.activate("weather-features-on")["status"] == "published"
    assert feature_enabled(conn, "weather-open-meteo") and feature_enabled(
        conn, "weather-verification"
    )
    coordinator.select("weather", bundles["weather"]["version"], features=[])
    coordinator.activate("weather-features-off")
    assert feature_enabled(conn, "weather-verification") is False


def test_the_projector_refuses_open_meteo_pages_when_the_feature_is_off():
    from src.kb.weather_store import WeatherError, WeatherProjector
    from tests.unit.weather import harness as h

    conn, _, _, _ = _migrated()
    source, _ = h.open_meteo(1781049600)
    with pytest.raises(WeatherError) as err:
        WeatherProjector(conn).project_page(
            run_id="r",
            manifest={"pack_id": "p", "version": "1"},
            source=source,
            records=[],
            documents=[],
            page_receipt={},
            principal_id="p",
        )
    assert err.value.code == "feature_disabled"
