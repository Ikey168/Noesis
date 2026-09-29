"""The OSS Ecosystems bundle, its providers and its optional features (#2203)."""

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
from src.composition.lifecycle import CompositionCoordinator
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.domains.pack_format import validate_manifest
from src.kb.oss_ecosystem_bundle import selection
from tools.knowledge_engine_mcp.oss_ecosystems import OSS_SCOPES, OSS_TOOLS

ROOT = Path(__file__).resolve().parents[3]
BASE = {
    "oss.registries",
    "oss.dependency-graphs",
    "oss.licences",
    "technology.core",
    "technology.vulnerabilities",
    "platform.entity-identity",
    "platform.subscriptions",
    "platform.source-runtime",
}
OSS = ("oss.registries", "oss.dependency-graphs", "oss.licences", "oss.archive")


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


def plan_for(features=None):
    bundles = adapt_all()
    root = {"pack": "oss-ecosystems", "version": bundles["oss-ecosystems"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {
        b["provider"] for b in plan["bindings"] if "oss-ecosystems" in b["consumers"]
    }


def test_the_v1_manifest_declares_exclusions_examples_and_the_source_pack():
    manifest = json.loads((ROOT / "packs/oss-ecosystems/pack.json").read_text())
    assert validate_manifest(manifest) == []
    assert manifest["source_pack"] == "config/source_packs/oss-ecosystems.json"
    exclusions = " ".join(manifest["exclusions"])
    for phrase in (
        "trust verdicts",
        "maintainer profiling",
        "scraping of individual",
        "duplicated vulnerability",
    ):
        assert phrase in exclusions
    assert all(
        e["semantics"] and e["tool"] in OSS_TOOLS for e in manifest["query_examples"]
    )
    assert validate_composition_manifest(adapt_all()["oss-ecosystems"]) == []


def test_descriptors_own_distinct_stores_and_expose_every_tool_with_its_scopes():
    descriptors = {d["id"]: d for d in provider_descriptors() if d["id"] in OSS}
    assert set(descriptors) == set(OSS)
    for descriptor in descriptors.values():
        assert validate_provider_descriptor(descriptor) == []
    tools = {
        o["tool"].rsplit(".", 1)[1]: o
        for d in descriptors.values()
        for o in d["operations"]
    }
    assert set(tools) == OSS_TOOLS
    for name, operation in tools.items():
        assert operation["required_scopes"] == (
            OSS_SCOPES[name] or ["knowledge:oss:read"]
        ), name
    stores = {s["store"]: d["id"] for d in descriptors.values() for s in d["stores"]}
    assert stores == {
        "src.kb.oss_ecosystem_store": "oss.registries",
        "src.kb.oss_ecosystem_identity": "oss.licences",
    }
    assert descriptors["oss.registries"]["source_packs"] == [
        {"pack_id": "oss-ecosystems", "version": "1.0.0", "range": "^1.0.0"}
    ]


def test_features_are_off_by_default_and_each_binds_its_own_provider():
    composition = json.loads(
        (ROOT / "packs/oss-ecosystems/composition.json").read_text()
    )
    assert [(f["id"], f["default"]) for f in composition["optional_features"]] == [
        ("oss-deps-dev", False),
        ("oss-software-heritage", False),
    ]
    required = {r["capability"] for r in composition["requires"]}
    assert {
        "technology.knowledge",
        "technology.vulnerabilities",
        "platform.entity-identity",
        "platform.subscriptions",
        "platform.source-acquisition",
    } <= required
    off = plan_for()
    assert off["features"]["oss-ecosystems"] == [] and bound(off) == BASE
    assert {
        "pack": "oss-ecosystems",
        "feature": "oss-software-heritage",
        "reason": "not selected",
    } in off["omissions"]
    deps = plan_for(["oss-deps-dev"])
    assert bound(deps) == BASE and any(
        b["capability"] == "oss.published-graphs" for b in deps["bindings"]
    )
    archive = plan_for(["oss-software-heritage"])
    assert bound(archive) == BASE | {"oss.archive"}
    both = plan_for(["oss-deps-dev", "oss-software-heritage"])
    assert not [o for o in both["omissions"] if o["pack"] == "oss-ecosystems"]


def test_selection_is_a_coordinator_change_that_keeps_technology():
    conn = duckdb.connect()
    coordinator = CompositionCoordinator(conn, legacy_config=lambda: [])
    bundles = adapt_all()
    for manifest in bundles.values():
        coordinator.install(manifest)
    for descriptor in provider_descriptors():
        coordinator.install(descriptor)
    coordinator.select("technology", bundles["technology"]["version"])
    coordinator.select(
        "oss-ecosystems",
        bundles["oss-ecosystems"]["version"],
        features=["oss-software-heritage"],
    )
    coordinator.activate("oss-on")
    assert selection(conn) == {
        "selected": True,
        "features": ["oss-software-heritage"],
        "authority": "composition-coordinator",
    }
    coordinator.disable("oss-ecosystems", "oss-off")
    plan = coordinator.active()["plan"]
    assert "oss-ecosystems" not in {p["id"] for p in plan["packs"]}
    assert "technology" in {p["id"] for p in plan["packs"]}
    assert selection(conn)["selected"] is False
