"""SS11 (#2798): the Society bundle's optional social-protection features and the ``society.social-protection`` provider."""

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
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.ingestion.source_packs import validate_source_pack
from src.kb.social_protection_records import enabled_providers, readiness
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.social_protection import (
    SOCIAL_PROTECTION_SCOPES,
    SOCIAL_PROTECTION_TOOLS,
    SOCIAL_PROTECTION_WRITES,
)

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = json.loads((ROOT / "packs/society/manifest.json").read_text())
PROVIDER = json.loads((ROOT / "packs/society/providers/society.social-protection.json").read_text())
PACK = {"pack_id": "society-social-protection", "version": "1.0.0", "range": "^1.0.0"}
FEATURES = ["esspros", "socx", "ilo-coverage"]
FEATURE_PROVIDERS = {"society.social-protection", "geospatial.core", "platform.entity-identity",
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


def society_plan(features=None):
    bundles = adapt_all()
    root = {"pack": "society", "version": bundles["society"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "society" in b["consumers"]}


def test_descriptor_validates_and_declares_capabilities_operations_stores_and_source_pack():
    assert validate_provider_descriptor(PROVIDER) == []
    descriptor = next(d for d in provider_descriptors() if d["id"] == "society.social-protection")
    assert descriptor == PROVIDER
    contracts = {c["id"]: c["contract"] for c in PROVIDER["capabilities"]}
    assert contracts["society.social-protection-series"] == {"name": "noesis-social-protection-record",
                                                            "version": "2.0.0"}
    series = next(c for c in PROVIDER["capabilities"] if c["id"] == "society.social-protection-series")
    constraints = series["semantic_constraints"]
    assert "no blending of ESSPROS, SOCX and ILO figures" in constraints["exclusions"]
    assert "no combination with COFOG" in constraints["exclusions"]
    assert "never presented as measuring the same thing" in constraints["comparability"]
    tools = {o["tool"].split(".", 1)[1]: o for o in PROVIDER["operations"]}
    assert set(tools) == SOCIAL_PROTECTION_TOOLS
    for name, operation in tools.items():
        assert operation["required_scopes"] == SOCIAL_PROTECTION_SCOPES[name]
        assert (operation["side_effect"] == "read-only") == (name not in SOCIAL_PROTECTION_WRITES)
    assert PROVIDER["source_packs"] == [PACK]
    tables = {t for s in PROVIDER["stores"] for t in s["tables"]}
    others = {t for d in provider_descriptors() if d["id"] != "society.social-protection"
              for s in d["stores"] for t in s["tables"]}
    assert all(t.startswith("social_protection_") for t in tables) and not tables & others
    assert all(s["store"].startswith("src.kb.social_protection_") for s in PROVIDER["stores"])


def test_the_manifest_contributes_default_off_features_per_source_and_a_version_bump():
    assert validate_composition_manifest(MANIFEST) == []
    assert MANIFEST["version"] == "0.2.0"  # 0.2.0 adds society.social-protection (#2741)
    features = {f["id"]: f for f in MANIFEST["optional_features"]}
    assert [(i, features[i]["default"]) for i in FEATURES] == [(i, False) for i in FEATURES]
    for feature in FEATURES:
        required = {r["capability"] for r in features[feature]["requires"]}
        assert {"society.social-protection-series", "society.social-protection-links",
                "geospatial.place-resolution", "platform.source-acquisition"} <= required
        assert "provider_absent" in features[feature]["description"]
    assert {"society.income", "society.social-protection"} == {p["id"] for p in MANIFEST["contributes"]["providers"]}
    assert PACK in MANIFEST["contributes"]["source_packs"]
    installed = validate_source_pack(json.loads((ROOT / "config/source_packs/society-social-protection.json")
                                                .read_text()))
    assert installed["version"] == PACK["version"]
    assert not list(ROOT.glob("packs/*social*"))  # no new pack


@pytest.mark.parametrize("selection", [[], ["esspros"], ["socx"], ["ilo-coverage"], FEATURES,
                                       ["pip", "esspros"]])
def test_each_source_feature_resolves_independently_and_together(selection):
    plan = society_plan(selection)
    social = bool(set(selection) & set(FEATURES))
    assert ("society.social-protection" in bound(plan)) == social
    if social:
        assert FEATURE_PROVIDERS <= bound(plan) and PACK in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.social_protection_indicator_for_place" in view.tools) == social
    assert "noesis-knowledge-engine.income_indicator_for_place" in view.tools


def test_feature_selection_and_readiness_degrade_links_to_provider_absent():
    report = readiness(duckdb.connect(":memory:"))
    assert report["optional_links"] == {"economics.demographics": "provider_absent",
                                        "economics.public-finance": "provider_absent"}
    assert {p: v["live_verification"]["status"] for p, v in report["providers"].items()} == {
        "eurostat-esspros": "unverified-live", "oecd-socx": "unverified-live",
        "ilo-social-protection-coverage": "unverified-live",
        "ilo-world-social-protection-dashboards": "not-implemented"}
    conn, coordinator, bundles, _ = _migrated()
    assert enabled_providers(conn) == set()  # composed with defaults: every social-protection feature is off
    coordinator.select("society", bundles["society"]["version"], features=["pip", "socx"])
    assert coordinator.activate("society-socx")["status"] == "published"
    assert enabled_providers(conn) == {"oecd-socx"}
    status = readiness(conn)
    assert status["providers"]["eurostat-esspros"]["status"] == "feature-disabled"
    assert status["providers"]["oecd-socx"]["status"] == "unavailable"


def test_tools_are_catalogued_with_declared_scopes_and_the_docs_carry_the_provider():
    for name in SOCIAL_PROTECTION_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in SOCIAL_PROTECTION_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == SOCIAL_PROTECTION_SCOPES[name]
    catalog = {t["id"]: t for t in json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json")
                                              .read_text())["tools"]}
    for name in SOCIAL_PROTECTION_TOOLS:
        assert catalog[f"noesis-knowledge-engine.{name}"]["required_scopes"] == SOCIAL_PROTECTION_SCOPES[name]
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "`society.social-protection`" in doc and "`noesis-social-protection-record-v2`" in doc


def test_taxonomy_classifies_the_provider_and_the_gap_row_is_removed():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["society.social-protection"] == {"subdomains": ["social-protection"],
                                                                  "shapes": ["statistical-series"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `social-protection` | Society and population |" not in program and "SS01 is covered offline" in program
    assert "(SS13)" in program  # live coverage is still open
