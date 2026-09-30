"""The News bundle's optional fact-checks features and the ``news.fact-checks`` provider (#2712)."""

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
from src.kb.fact_checks_records import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.fact_checks import FACT_CHECK_SCOPES

ROOT = Path(__file__).resolve().parents[3]
FEATURES = ("fact-checks-google", "fact-checks-datacommons", "fact-checks-ifcn")
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


def news_plan(features=None, bundles=None):
    bundles = bundles or adapt_all()
    root = {"pack": "news", "version": bundles["news"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "news" in b["consumers"]}


def descriptor():
    return next(d for d in provider_descriptors() if d["id"] == "news.fact-checks")


def test_descriptor_declares_operations_scopes_stores_exclusions_and_the_source_pack():
    found = descriptor()
    assert validate_provider_descriptor(found) == []
    assert (ROOT / "packs/news/providers/news.fact-checks.json").exists()
    (capability,) = found["capabilities"]
    assert capability["contract"] == {"name": "noesis-fact-check-record", "version": "1.0.0"}
    constraints = capability["semantic_constraints"]
    assert "no truth verdicts by Noesis" in constraints["exclusions"]
    assert "no automatic claim matching without review" in constraints["exclusions"]
    assert "FC01" in constraints["minimisation"]
    tools = {op["tool"].rsplit(".", 1)[1]: op for op in found["operations"]}
    assert set(tools) == set(FACT_CHECK_SCOPES)
    for name, op in tools.items():
        assert op["required_scopes"] == (FACT_CHECK_SCOPES[name] or ["knowledge:read"])
    assert found["source_packs"] == [SOURCE_PACK]
    owned = {s["record_type"] for s in found["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "news.fact-checks" for s in d["stores"]}
    assert not owned & others  # no second entity, subscription, claim or source-identity store
    assert json.loads((ROOT / "packs/news/providers/news.core.json").read_text())["id"] == "news.core"


def test_the_three_sources_are_separate_optional_features_off_by_default():
    composition = json.loads((ROOT / "packs/news/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(FEATURES) <= set(features)
    for feature in FEATURES:
        assert features[feature]["default"] is False
        required = {r["capability"] for r in features[feature]["requires"]}
        assert "news.fact-checks" in required
        # news articles, OSINT corroboration and claim links degrade when absent, so no feature requires them
        assert not required & {"osint.corroboration", "osint.media-provenance", "science.literature"}
    assert validate_composition_manifest(adapt_all()["news"]) == []
    plan = news_plan()
    assert "news.fact-checks" not in bound(plan)
    assert set(FEATURES) <= {o["feature"] for o in plan["omissions"] if o["pack"] == "news"}
    assert not list(ROOT.glob("packs/*fact*"))  # no new pack


@pytest.mark.parametrize("selection", [["fact-checks-google"], ["fact-checks-datacommons"], ["fact-checks-ifcn"],
                                       list(FEATURES)])
def test_selecting_a_feature_binds_the_provider_and_the_platform_capabilities(selection):
    plan = news_plan(selection)
    assert sorted(plan["features"]["news"]) == sorted(selection)
    capabilities = {b["capability"] for b in plan["bindings"]}
    assert {"news.fact-checks", "platform.entity-identity", "platform.subscriptions",
            "platform.source-acquisition"} <= capabilities
    assert "news.fact-checks" in bound(plan)
    # the bounded-public-osint source pack stays the Osint bundle's; news.fact-checks pins it in its descriptor only
    assert SOURCE_PACK not in plan["source_packs"] and descriptor()["source_packs"] == [SOURCE_PACK]


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert not any(feature_enabled(conn, f) for f in FEATURES)
    coordinator.select("news", bundles["news"]["version"], features=["fact-checks-ifcn"])
    assert coordinator.activate("news-fact-checks-ifcn-on")["status"] == "published"
    assert feature_enabled(conn, "fact-checks-ifcn")
    assert not feature_enabled(conn, "fact-checks-google") and not feature_enabled(conn, "fact-checks-datacommons")


def test_the_taxonomy_classifies_the_provider_and_the_gap_row_is_gone():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["news.fact-checks"] == {"subdomains": ["fact-checks"],
                                                        "shapes": ["versioned-documents", "registry-records"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `fact-checks` |" not in program
