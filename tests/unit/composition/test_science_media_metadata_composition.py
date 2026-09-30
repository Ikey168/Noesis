"""The Science bundle's optional ``media-metadata`` features and the ``science.media-metadata`` provider (MM11, #2508)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.media_metadata import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.cultural import MEDIA_READS, MEDIA_SCOPES, MEDIA_TOOLS

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {"science.media-metadata", "science.cultural", "platform.entity-identity",
                     "platform.subscriptions", "platform.source-runtime"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def science_plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "science", "version": bundles["science"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()),
                     descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "science" in b["consumers"]}


def test_descriptor_declares_capabilities_constraints_operations_stores_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "science.media-metadata")
    assert validate_provider_descriptor(descriptor) == []
    capabilities = {c["id"]: c for c in descriptor["capabilities"]}
    assert set(capabilities) == {"science.media-metadata", "science.media-news-candidates"}
    assert capabilities["science.media-metadata"]["contract"] == {"name": "noesis-media-metadata-record",
                                                                  "version": "1.0.0"}
    assert set(capabilities["science.media-metadata"]["semantic_constraints"]) == {
        "levels", "revisions", "identity", "links", "licences", "exclusions"}
    catalog = {t["name"]: t for t in json.loads(
        (ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())["tools"]}
    names = set()
    for operation in descriptor["operations"]:
        name = operation["tool"].split(".", 1)[1]
        names.add(name)
        assert operation["required_scopes"] == MEDIA_SCOPES[name] == catalog[name]["required_scopes"], name
        assert (operation["side_effect"] == "read-only") == (name in MEDIA_READS), name
    assert names == MEDIA_TOOLS
    assert descriptor["source_packs"] == [{"pack_id": "primary-scientific-evidence", "version": "1.2.0",
                                           "range": "^1.2.0"}]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "science.media-metadata"
              for s in d["stores"]}
    assert owned == {"media-metadata-record", "media-metadata-refresh-receipt"} and not owned & others


def test_the_features_are_off_by_default_and_the_bundle_validates():
    composition = json.loads((ROOT / "packs/science/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert {k: f["default"] for k, f in features.items()} == {
        "cultural-collections": False, "education-statistics": False, "media-metadata": False,
        "media-metadata-news": False, "life-sciences-chembl": False, "life-sciences-ncbi": False,
        "life-sciences-pdb": False, "life-sciences-uniprot": False}
    assert {r["capability"] for r in features["media-metadata"]["requires"]} == {
        "science.media-metadata", "science.cultural-objects", "platform.entity-identity", "platform.subscriptions",
        "platform.source-acquisition"}
    assert {r["capability"] for r in features["media-metadata-news"]["requires"]} == {
        "science.media-news-candidates", "science.media-metadata", "news.articles", "platform.entity-identity"}
    assert validate_composition_manifest(adapt_all()["science"]) == []
    plan = science_plan()
    assert plan["features"]["science"] == []
    assert not {"science.media-metadata", "science.cultural"} & bound(plan)
    omitted = {o["feature"] for o in plan["omissions"] if o["pack"] == "science"}
    assert omitted == {"cultural-collections", "education-statistics", "media-metadata", "media-metadata-news",
                       "life-sciences-chembl", "life-sciences-ncbi", "life-sciences-pdb", "life-sciences-uniprot"}
    pack = json.loads((ROOT / "packs/science/pack.json").read_text())
    assert pack["schema_versions"]["media-metadata-record"] == "1.0.0"
    assert {e["tool"] for e in pack["query_examples"]} >= {"resolve_media_identifier", "search_media_titles"}
    assert {"pack_id": "primary-scientific-evidence", "version": "1.2.0", "range": "^1.2.0"} in plan["source_packs"]


def test_selecting_the_features_binds_their_providers_one_per_capability():
    plan = science_plan(["media-metadata"])
    assert plan["features"]["science"] == ["media-metadata"] and FEATURE_PROVIDERS <= bound(plan)
    assert "news.core" not in bound(plan)
    bindings = [b for b in plan["bindings"] if "science" in b["consumers"]]
    assert len({b["capability"] for b in bindings}) == len(bindings)
    assert {o["feature"] for o in plan["omissions"] if o["pack"] == "science"} == {
        "cultural-collections", "education-statistics", "media-metadata-news", "life-sciences-chembl", "life-sciences-ncbi", "life-sciences-pdb", "life-sciences-uniprot"}
    news = science_plan(["media-metadata", "media-metadata-news"])
    assert {"science.media-metadata", "news.core"} <= bound(news)
    everything = science_plan(["cultural-collections", "education-statistics", "media-metadata", "media-metadata-news",
                               "life-sciences-chembl", "life-sciences-ncbi", "life-sciences-pdb", "life-sciences-uniprot"])
    assert not [o for o in everything["omissions"] if o["pack"] == "science"]


def test_a_missing_consumed_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "platform.subscriptions"]
    plan = science_plan(["media-metadata"], descriptors)
    assert plan["features"]["science"] == []
    omission = next(o for o in plan["omissions"] if o["feature"] == "media-metadata")
    assert omission["capability"] == "platform.subscriptions" and "missing_contract" in omission["reason"]


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("science", bundles["science"]["version"], features=["media-metadata"])
    assert coordinator.activate("science-media-on")["status"] == "published"
    assert feature_enabled(conn) is True and feature_enabled(conn, "media-metadata-news") is False
    coordinator.select("science", bundles["science"]["version"], features=[])
    coordinator.activate("science-media-off")
    assert feature_enabled(conn) is False
