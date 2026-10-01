"""The osint.platform-transparency provider and the OSINT bundle's optional platform-transparency features (#2641)."""

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
SOURCE_PACK = {"pack_id": "bounded-public-osint", "version": "1.2.0", "range": "^1.2.0"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def osint_plan(features=None, bundles=None):
    bundles = bundles or adapt_all()
    root = {"pack": "osint", "version": bundles["osint"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["capability"] for b in plan["bindings"]}


def descriptor():
    return next(d for d in provider_descriptors() if d["id"] == "osint.platform-transparency")


def test_descriptor_validates_declares_scopes_stores_exclusions_and_pins_the_source_pack():
    found = descriptor()
    assert validate_provider_descriptor(found) == []
    assert (ROOT / "packs/osint/providers/osint.platform-transparency.json").exists()
    (capability,) = found["capabilities"]
    assert capability["contract"] == {"name": "noesis-platform-transparency-record", "version": "1.0.0"}
    constraints = capability["semantic_constraints"]
    assert "no user-level profiling" in constraints["exclusions"]
    assert "no conversion of spend or impression ranges into point estimates" in constraints["exclusions"]
    assert "SP01" in constraints["minimisation"] and "never targets" in constraints["matching"]
    tools = {op["tool"].rsplit(".", 1)[1]: op for op in found["operations"]}
    for name, op in tools.items():
        assert op["required_scopes"] == PLATFORM_TRANSPARENCY_SCOPES[name]
    assert found["source_packs"] == [SOURCE_PACK]
    owned = {s["record_type"] for s in found["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "osint.platform-transparency"
              for s in d["stores"]}
    assert not owned & others
    # existing OSINT providers are unchanged
    for provider in ("osint.core", "osint.movements"):
        assert json.loads((ROOT / f"packs/osint/providers/{provider}.json").read_text())["id"] == provider


def test_dsa_meta_google_and_lumen_are_separate_optional_features_off_by_default():
    composition = json.loads((ROOT / "packs/osint/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    for feature in FEATURES:
        assert features[feature]["default"] is False
        required = {r["capability"] for r in features[feature]["requires"]}
        assert "osint.platform-transparency" in required
        # elections, campaign finance and lobbying links degrade when absent, so no feature requires them
        assert not required & {"political.elections", "political.campaign-finance", "political.lobbying"}
    assert "not implemented" in features["platform-transparency-lumen"]["description"]
    assert validate_composition_manifest(adapt_all()["osint"]) == []
    default = osint_plan()
    assert "osint.platform-transparency" not in bound(default)
    omitted = {o["feature"] for o in default["omissions"] if o["pack"] == "osint"}
    assert set(FEATURES) <= omitted


@pytest.mark.parametrize("selection", [[f] for f in FEATURES] + [list(FEATURES), ["movements",
                                                                                  "platform-transparency-meta"]])
def test_selecting_a_feature_binds_the_provider(selection):
    plan = osint_plan(selection)
    assert "osint.platform-transparency" in bound(plan)
    if selection != ["platform-transparency-lumen"]:
        assert {"platform.subscriptions", "platform.source-acquisition"} <= bound(plan)
    if "platform-transparency-meta" in selection:
        assert "ownership.identity" in bound(plan)
    assert ("osint.movements" in bound(plan)) == ("movements" in selection)


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert not any(feature_enabled(conn, f) for f in FEATURES)
    coordinator.select("osint", bundles["osint"]["version"], features=["platform-transparency-dsa"])
    assert coordinator.activate("osint-platform-transparency-dsa-on")["status"] == "published"
    assert feature_enabled(conn, "platform-transparency-dsa")
    assert not feature_enabled(conn, "platform-transparency-meta")


def test_pack_manifest_taxonomy_and_source_pack_record_the_provider_without_a_new_pack():
    pack = json.loads((ROOT / "packs/osint/pack.json").read_text())
    assert "platform-transparency-records-and-political-ad-libraries" in pack["capabilities"]
    assert pack["schema_versions"]["platform-transparency-record"] == "1.0.0"
    assert {"user-level profiling of platform users or ad viewers",
            "inference of coordinated behaviour from ad or moderation records",
            "conversion of spend or impression ranges into point estimates"} <= set(pack["exclusions"])
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["osint.platform-transparency"] == {
        "subdomains": ["social-platforms"], "shapes": ["events-notices", "registry-records"],
        "themes": ["security-defence"]}
    roadmap = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `social-platforms` |" not in roadmap
    source_pack = json.loads((ROOT / "config/source_packs/osint.json").read_text())
    assert source_pack["version"] == "1.2.0"
    assert {s["connector"] for s in source_pack["sources"] if s["source_id"].startswith("platform-transparency-")} == {
        "platform-transparency"}
    assert not list(ROOT.glob("packs/*platform-transparency*"))  # no new pack
