"""#2882: the Society bundle's optional wealth features and the ``society.wealth`` provider."""

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
from src.kb.wealth_distribution_records import enabled_providers, readiness
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.wealth import WEALTH_SCOPES, WEALTH_TOOLS, WEALTH_WRITES

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = json.loads((ROOT / "packs/society/manifest.json").read_text())
PROVIDER = json.loads((ROOT / "packs/society/providers/society.wealth.json").read_text())
PACK = {"pack_id": "society-wealth-distribution", "version": "1.0.0", "range": "^1.0.0"}
FEATURES = ["wid", "dwa", "dfa"]


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


def test_descriptor_validates_and_declares_the_series_capability_operations_stores_and_pack():
    assert validate_provider_descriptor(PROVIDER) == []
    assert next(d for d in provider_descriptors() if d["id"] == "society.wealth") == PROVIDER
    series = next(c for c in PROVIDER["capabilities"] if c["id"] == "society.wealth-series")
    assert series["contract"] == {"name": "noesis-wealth-distribution-record", "version": "2.0.0"}
    assert "no blending of WID, DWA and DFA figures" in series["semantic_constraints"]["exclusions"]
    assert "comparability_unknown" in series["semantic_constraints"]["comparability"]
    tools = {o["tool"].split(".", 1)[1]: o for o in PROVIDER["operations"]}
    assert set(tools) == WEALTH_TOOLS
    for name, operation in tools.items():
        assert operation["required_scopes"] == WEALTH_SCOPES[name]
        assert (operation["side_effect"] == "read-only") == (name not in WEALTH_WRITES)
    assert PROVIDER["source_packs"] == [PACK]
    tables = {t for s in PROVIDER["stores"] for t in s["tables"]}
    others = {t for d in provider_descriptors() if d["id"] != "society.wealth" for s in d["stores"]
              for t in s["tables"]}
    assert all(t.startswith("wealth_") for t in tables) and not tables & others


def test_the_manifest_contributes_default_off_features_per_source():
    assert validate_composition_manifest(MANIFEST) == []
    features = {f["id"]: f for f in MANIFEST["optional_features"]}
    assert [(i, features[i]["default"]) for i in FEATURES] == [(i, False) for i in FEATURES]
    for feature in FEATURES:
        assert {r["capability"] for r in features[feature]["requires"]} == {"society.wealth-series",
                                                                           "platform.source-acquisition"}
    assert "society.wealth" in {p["id"] for p in MANIFEST["contributes"]["providers"]}
    assert PACK in MANIFEST["contributes"]["source_packs"]
    installed = validate_source_pack(json.loads((ROOT / "config/source_packs/society-wealth-distribution.json")
                                                .read_text()))
    assert installed["version"] == PACK["version"]
    assert not list(ROOT.glob("packs/*wealth*"))  # no new pack: the Society bundle carries the provider


@pytest.mark.parametrize("selection", [[], ["wid"], ["dwa"], ["dfa"], FEATURES, ["pip", "wid"]])
def test_each_source_feature_resolves_independently_and_together(selection):
    plan = society_plan(selection)
    wealth = bool(set(selection) & set(FEATURES))
    assert ("society.wealth" in bound(plan)) == wealth
    if wealth:
        assert PACK in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.wealth_indicator_for_place" in view.tools) == wealth


def test_feature_selection_drives_readiness():
    report = readiness(duckdb.connect(":memory:"))
    assert {p: v["live_verification"]["status"] for p, v in report["providers"].items()} == {
        "wid-wealth-distribution": "unverified-live", "ecb-distributional-wealth-accounts": "unverified-live",
        "fed-distributional-financial-accounts": "unverified-live",
        "ecb-hfcs-statistical-tables": "not-implemented", "bundesbank-phf-aggregates": "not-implemented"}
    conn, coordinator, bundles, _ = _migrated()
    assert enabled_providers(conn) == set()  # composed with defaults: every wealth feature is off
    coordinator.select("society", bundles["society"]["version"], features=["pip", "dfa"])
    assert coordinator.activate("society-dfa")["status"] == "published"
    assert enabled_providers(conn) == {"fed-distributional-financial-accounts"}
    status = readiness(conn)
    assert status["providers"]["wid-wealth-distribution"]["status"] == "feature-disabled"
    assert status["providers"]["fed-distributional-financial-accounts"]["status"] == "unavailable"


def test_tools_are_catalogued_with_declared_scopes_and_the_docs_carry_the_provider():
    for name in WEALTH_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in WEALTH_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == WEALTH_SCOPES[name]
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "`society.wealth`" in doc and "`noesis-wealth-distribution-record-v2`" in doc


def test_taxonomy_classifies_the_provider_under_an_existing_subdomain():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["society.wealth"] == {"subdomains": ["income-poverty-inequality"],
                                                       "shapes": ["statistical-series"]}
    depth = json.loads((ROOT / "packs/depth.json").read_text())
    assert depth["providers"]["society.wealth"]["sources"] == [
        "wid-wealth-distribution", "ecb-distributional-wealth-accounts", "fed-distributional-financial-accounts"]
