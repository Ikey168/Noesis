"""The Sports bundle: pack, composition, provider descriptors and optional features (#2146, SP11)."""

from __future__ import annotations

import json

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.sports_bundle import readiness
from src.kb.sports_records import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tests.unit.sports import harness as h

SPORTS_PROVIDERS = {
    "sports.football",
    "sports.tennis",
    "sports.olympics",
    "sports.forecasts",
}
CORE = {
    "sports.football",
    "sports.olympics",
    "geospatial.core",
    "news.core",
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


def plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "sports", "version": bundles["sports"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve(
        [root],
        list(bundles.values()),
        descriptors if descriptors is not None else provider_descriptors(),
    )
    assert result.ok, result.failure
    return result.plan


def bound(value):
    return {b["provider"] for b in value["bindings"] if "sports" in b["consumers"]}


def test_descriptors_validate_and_own_their_stores_without_a_second_forecast_or_entity_store():
    descriptors = {
        d["id"]: d for d in provider_descriptors() if d["id"] in SPORTS_PROVIDERS
    }
    assert set(descriptors) == SPORTS_PROVIDERS
    for descriptor in descriptors.values():
        assert validate_provider_descriptor(descriptor) == []
        assert descriptor["source_packs"] == [
            {"pack_id": "sports-records", "version": "1.0.0", "range": "^1.0.0"}
        ]
        assert descriptor["readiness_probes"] == [
            {"id": "records", "kind": "table-exists", "target": "sports_revisions"}
        ]
    stores = {s["store"] for d in descriptors.values() for s in d["stores"]}
    assert stores == {
        "src.kb.sports_store",
        "src.kb.sports_identity",
        "src.kb.sports_links",
        "src.kb.sports_forecasts",
    }
    tables = {t for d in descriptors.values() for s in d["stores"] for t in s["tables"]}
    assert all(t.startswith("sports_") for t in tables)
    tools = {
        o["tool"].split(".", 1)[1]
        for d in descriptors.values()
        for o in d["operations"]
    }
    assert {
        "sports_standings_as_of",
        "sports_match_history",
        "sports_fixture_history",
        "sports_team_schedule",
        "propose_sports_identity_matches",
        "register_sports_forecast",
        "create_sports_monitor",
    } <= tools


def test_the_pack_declares_capabilities_schemas_ontology_source_pack_examples_and_exclusions():
    pack = json.loads((h.ROOT / "packs/sports/pack.json").read_text())
    assert (
        pack["pack_format"] == "noesis-pack-v1"
        and pack["source_pack"] == "config/source_packs/sports.json"
    )
    assert {"tables-as-of-a-date", "result-revisions-with-corrections"} <= set(
        pack["capabilities"]
    )
    assert pack["schema_versions"]["sports-record"] == "1.0.0"
    assert {"fixture", "match_result_revision", "standing_snapshot"} <= set(
        pack["ontology_extensions"]["object_types"]
    )
    assert all(example["semantics"] for example in pack["query_examples"])
    exclusions = " ".join(pack["exclusions"])
    for phrase in (
        "odds",
        "betting advice",
        "medical",
        "biometric",
        "personal data beyond published sporting records",
    ):
        assert phrase in exclusions
    assert validate_composition_manifest(adapt_all()["sports"]) == []


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        (None, CORE),
        (["sports-tennis"], CORE | {"sports.tennis"}),
        (["sports-forecasts"], CORE | {"sports.forecasts"}),
        (["sports-identity"], CORE | {"ownership.core"}),
        (
            ["sports-identity", "sports-tennis", "sports-forecasts"],
            CORE | {"ownership.core", "sports.tennis", "sports.forecasts"},
        ),
    ],
)
def test_the_bundle_resolves_with_each_feature_on_and_off(features, expected):
    value = plan(features)
    assert bound(value) == expected
    assert sorted(value["features"].get("sports") or []) == sorted(features or [])
    assert {
        "pack_id": "sports-records",
        "version": "1.0.0",
        "range": "^1.0.0",
    } in value["source_packs"]
    if not features:
        omitted = {
            o["feature"] for o in value["omissions"] if o.get("pack") == "sports"
        }
        assert omitted == {"sports-identity", "sports-tennis", "sports-forecasts"}


def test_features_default_off_and_follow_the_active_composition_selection():
    composition = json.loads((h.ROOT / "packs/sports/composition.json").read_text())
    assert {f["id"]: f["default"] for f in composition["optional_features"]} == {
        "sports-identity": False,
        "sports-tennis": False,
        "sports-forecasts": False,
    }
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn, "sports-tennis") is False
    coordinator.select(
        "sports", bundles["sports"]["version"], features=["sports-tennis"]
    )
    assert coordinator.activate("sports-tennis-on")["status"] == "published"
    assert (
        feature_enabled(conn, "sports-tennis") is True
        and feature_enabled(conn, "sports-forecasts") is False
    )
    status = readiness(conn)
    assert status["features"] == {
        "sports-identity": False,
        "sports-tennis": True,
        "sports-forecasts": False,
    }
    assert (
        status["status"] == "not_ready"
        and status["providers"]["bookmakers"]["access_decision"] == "not implemented"
    )


def test_a_missing_geospatial_provider_is_a_visible_failure_of_the_core_bundle():
    bundles = adapt_all()
    descriptors = [d for d in provider_descriptors() if d["id"] != "geospatial.core"]
    result = resolve(
        [{"pack": "sports", "version": bundles["sports"]["version"]}],
        list(bundles.values()),
        descriptors,
    )
    assert not result.ok and "geospatial" in str(result.failure)
