"""The osint.platform-transparency provider and the Osint bundle's optional platform-transparency features (#2641)."""

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
from src.kb.platform_transparency_records import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.platform_transparency import (
    PLATFORM_TRANSPARENCY_SCOPES,
)

ROOT = Path(__file__).resolve().parents[3]
FEATURES = ("platform-transparency-dsa", "platform-transparency-meta", "platform-transparency-google",
            "platform-transparency-lumen")
SOURCE_PACK = {"pack_id": "osint-platform-transparency", "version": "1.0.0", "range": "^1.0.0"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def osint_plan(features=None):
    bundles = adapt_all()
    root = {"pack": "osint", "version": bundles["osint"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def descriptor():
    return next(d for d in provider_descriptors() if d["id"] == "osint.platform-transparency")


def test_descriptor_validates_declares_scopes_stores_exclusions_and_the_source_pack():
    found = descriptor()
    assert validate_provider_descriptor(found) == []
    assert (ROOT / "packs/osint/providers/osint.platform-transparency.json").exists()
    (capability,) = found["capabilities"]
    assert capability["contract"] == {"name": "noesis-platform-transparency-record", "version": "2.0.0"}
    constraints = capability["semantic_constraints"]
    assert "no user-level profiling" in constraints["exclusions"] and "point estimates" in constraints["exclusions"]
    assert "platform-transparency-minimisation-v1" in constraints["minimisation"]
    assert "gated-not-granted" in constraints["gated"]
    tools = {op["tool"].rsplit(".", 1)[1]: op for op in found["operations"]}
    assert set(tools) == set(PLATFORM_TRANSPARENCY_SCOPES)
    for name, op in tools.items():
        assert op["required_scopes"] == (PLATFORM_TRANSPARENCY_SCOPES[name] or ["knowledge:read"])
    assert found["source_packs"] == [SOURCE_PACK]
    owned = {s["record_type"] for s in found["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != found["id"] for s in d["stores"]}
    assert not owned & others  # no second identity, subscription or ownership store
    for provider in ("osint.core", "osint.movements"):  # existing providers unchanged
        assert json.loads((ROOT / f"packs/osint/providers/{provider}.json").read_text())["id"] == provider


def test_each_source_is_a_separate_optional_feature_off_by_default():
    composition = json.loads((ROOT / "packs/osint/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    for feature in FEATURES:
        assert features[feature]["default"] is False
        required = {r["capability"] for r in features[feature]["requires"]}
        assert "osint.platform-transparency" in required
        # elections, campaign-finance, lobbying and ownership links degrade when absent: never required
        assert not required & {"political.election-records", "political.campaign-finance", "political.lobbying",
                               "ownership.graph"}
    assert validate_composition_manifest(adapt_all()["osint"]) == []
    default = osint_plan()
    assert "osint.platform-transparency" not in {b["capability"] for b in default["bindings"]}
    # the pack is pinned by the provider only (as geospatial-infrastructure is), never by the bundle, so a default
    # Osint plan neither binds the provider nor contributes the pack
    assert SOURCE_PACK not in default["source_packs"]
    assert set(FEATURES) <= {o["feature"] for o in default["omissions"] if o["pack"] == "osint"}


@pytest.mark.parametrize("feature", FEATURES)
def test_selecting_a_feature_binds_the_provider_identity_subscriptions_and_the_source_pack(feature):
    plan = osint_plan([feature])
    capabilities = {b["capability"] for b in plan["bindings"]}
    assert {"osint.platform-transparency", "ownership.identity", "platform.subscriptions",
            "platform.source-acquisition"} <= capabilities
    bound = next(b for b in plan["bindings"] if b["capability"] == "osint.platform-transparency")
    assert bound["provider"] == "osint.platform-transparency" and descriptor()["source_packs"] == [SOURCE_PACK]


def test_feature_enablement_follows_the_active_selection_and_no_new_pack_exists():
    conn, coordinator, bundles, _ = _migrated()
    assert not any(feature_enabled(conn, f) for f in FEATURES)
    coordinator.select("osint", bundles["osint"]["version"], features=["platform-transparency-dsa"])
    assert coordinator.activate("osint-platform-transparency-dsa-on")["status"] == "published"
    assert feature_enabled(conn, "platform-transparency-dsa")
    assert not feature_enabled(conn, "platform-transparency-meta")
    pack = json.loads((ROOT / "packs/osint/pack.json").read_text())
    assert pack["schema_versions"]["platform-transparency-record"] == "2.0.0"
    assert {"user-level profiling from platform transparency data",
            "conversion of ad spend or impression ranges into point estimates"} <= set(pack["exclusions"])
    assert not list(ROOT.glob("packs/*platform-transparency*"))  # no new pack
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    entry = taxonomy["providers"]["osint.platform-transparency"]
    assert entry == {"subdomains": ["social-platforms"], "shapes": ["events-notices", "registry-records"],
                     "themes": ["security-defence"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `social-platforms` |" not in program
