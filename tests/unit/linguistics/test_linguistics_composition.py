"""The Linguistics bundle: manifests, providers, optional features and enablement (LG11, #2189)."""

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
from src.kb.linguistics_bundle import EXCLUSIONS, FEATURES, feature_enabled
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
BASE = {
    "linguistics.lexicon",
    "linguistics.languoids",
    "platform.cross-language",
    "platform.entity-identity",
    "platform.subscriptions",
    "platform.source-runtime",
    "geospatial.core",
    "science.literature",
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
    root = {"pack": "linguistics", "version": bundles["linguistics"]["version"]}
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
    return {b["provider"] for b in value["bindings"] if "linguistics" in b["consumers"]}


def test_v1_manifest_declares_capabilities_query_examples_and_the_tracker_exclusions():
    pack = json.loads((ROOT / "packs/linguistics/pack.json").read_text())
    assert (
        validate_manifest(pack) == []
        and pack["source_pack"] == "config/source_packs/linguistics.json"
    )
    assert {
        "lookup_lexeme",
        "sense_history",
        "lexeme_etymology",
        "languoid_profile",
    } == {q["tool"] for q in pack["query_examples"]}
    assert all(q["semantics"] for q in pack["query_examples"])
    text = " ".join(pack["exclusions"])
    assert "machine-translated" in text and "speaker personal data" in text
    assert set(pack["exclusions"]) == set(EXCLUSIONS)
    assert validate_composition_manifest(adapt_all()["linguistics"]) == []


def test_descriptors_declare_capabilities_stores_probes_and_one_owner_each():
    descriptors = {d["id"]: d for d in provider_descriptors()}
    for provider in (
        "linguistics.lexicon",
        "linguistics.languoids",
        "linguistics.typology",
        "platform.cross-language",
    ):
        assert validate_provider_descriptor(descriptors[provider]) == [], provider
    lexicon = descriptors["linguistics.lexicon"]
    assert lexicon["source_packs"] == [
        {
            "pack_id": "linguistics-lexical-typological",
            "version": "1.0.0",
            "range": "^1.0.0",
        }
    ]
    assert lexicon["stores"][0]["tables"] == [
        "ling_bodies",
        "ling_sightings",
        "ling_receipts",
    ]
    cross = descriptors["platform.cross-language"]
    assert (
        cross["stores"][0]["store"] == "src.kb.cross_language"
    )  # the existing owner, no parallel store
    tools = {
        o["tool"].split(".", 1)[1]
        for p in (
            "linguistics.lexicon",
            "linguistics.languoids",
            "linguistics.typology",
        )
        for o in descriptors[p]["operations"]
    }
    assert {
        "lookup_lexeme",
        "sense_history",
        "lexeme_etymology",
        "languoid_profile",
        "propose_languoid_matches",
        "propose_lexeme_matches",
        "create_linguistics_monitor",
    } <= tools
    owners = {}
    for descriptor in descriptors.values():
        for store in descriptor["stores"]:
            owners.setdefault(store["record_type"], set()).add(descriptor["id"])
    assert all(len(v) == 1 for v in owners.values())


def test_features_default_off_and_each_composes_on_and_off():
    composition = json.loads((ROOT / "packs/linguistics/composition.json").read_text())
    assert {f["id"]: f["default"] for f in composition["optional_features"]} == {
        f: False for f in FEATURES
    }
    off = plan()
    assert off["features"]["linguistics"] == [] and bound(off) == BASE
    assert {
        "pack": "linguistics",
        "feature": "linguistics-typology",
        "reason": "not selected",
    } in off["omissions"]
    wiktionary = plan(["linguistics-wiktionary"])
    assert (
        wiktionary["features"]["linguistics"] == ["linguistics-wiktionary"]
        and bound(wiktionary) == BASE
    )
    typology = plan(["linguistics-typology"])
    assert bound(typology) == BASE | {"linguistics.typology"}
    both = plan(list(FEATURES))
    assert sorted(both["features"]["linguistics"]) == sorted(FEATURES)
    assert {
        "pack_id": "linguistics-lexical-typological",
        "version": "1.0.0",
        "range": "^1.0.0",
    } in both["source_packs"]


def test_a_missing_shipped_provider_fails_resolution_explicitly():
    bundles = adapt_all()
    descriptors = [
        d for d in provider_descriptors() if d["id"] != "linguistics.typology"
    ]
    result = resolve(
        [
            {
                "pack": "linguistics",
                "version": bundles["linguistics"]["version"],
                "features": ["linguistics-typology"],
            }
        ],
        list(bundles.values()),
        descriptors,
    )
    assert not result.ok and result.failure.code == "missing_provider"
    assert result.failure.details["provider"] == "linguistics.typology"


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn, "linguistics-wiktionary") is False
    coordinator.select(
        "linguistics",
        bundles["linguistics"]["version"],
        features=["linguistics-wiktionary"],
    )
    assert coordinator.activate("linguistics-wiktionary-on")["status"] == "published"
    assert feature_enabled(conn, "linguistics-wiktionary") is True
    assert feature_enabled(conn, "linguistics-typology") is False
    coordinator.select("linguistics", bundles["linguistics"]["version"], features=[])
    coordinator.activate("linguistics-wiktionary-off")
    assert feature_enabled(conn, "linguistics-wiktionary") is False
