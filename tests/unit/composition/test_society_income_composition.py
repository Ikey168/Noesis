"""IP11 (#2637): the new Society bundle, the society.income provider, its optional features and MCP tools."""

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
from src.evidence_bundle.verifier import verify_bundle
from src.kb.society_bundle import (
    BUNDLE,
    BundleError,
    enabled_providers,
    readiness,
    require_enabled,
)
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import income_distribution_harness as h
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.society_income import INCOME_TOOLS, INCOME_WRITES

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = json.loads((ROOT / "packs/society/manifest.json").read_text())
PROVIDER = json.loads((ROOT / "packs/society/providers/society.income.json").read_text())
SHARED = {"geospatial.place-resolution", "platform.entity-identity", "platform.subscriptions",
          "platform.source-acquisition"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def plan_for(features=None):
    bundles = adapt_all()
    root = {"pack": "society", "version": bundles["society"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def test_manifest_and_provider_validate_and_declare_the_taxonomy_cell():
    assert validate_composition_manifest(MANIFEST) == []
    assert validate_provider_descriptor(PROVIDER) == []
    assert SHARED <= {r["capability"] for r in MANIFEST["requires"]}
    assert [(f["id"], f["default"]) for f in MANIFEST["optional_features"]][:3] == [
        ("pip", True), ("eu-silc", True), ("oecd-idd", True)]  # then the social-protection features (#2741)
    assert MANIFEST["advisory"]["exclusions"] == BUNDLE["exclusions"]
    tools = {o["tool"].split(".", 1)[1] for o in PROVIDER["operations"]}
    assert tools == INCOME_TOOLS - {"set_society_bundle_enabled"}
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["packs"]["society"] == {"domain": "society-population"}
    assert taxonomy["providers"]["society.income"] == {"subdomains": ["income-poverty-inequality"],
                                                       "shapes": ["statistical-series"]}
    gaps = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `income-poverty-inequality` |" not in gaps


def test_the_plan_binds_its_own_provider_plus_shared_providers_with_one_store_owner():
    plan = plan_for()
    bound = {b["provider"] for b in plan["bindings"] if "society" in b["consumers"]}
    assert {"society.income", "geospatial.core", "platform.entity-identity", "platform.subscriptions",
            "platform.source-runtime"} <= bound
    owned = {s["store"] for d in provider_descriptors() if d["id"] == "society.income" for s in d["stores"]}
    tables = {t for d in provider_descriptors() if d["id"] == "society.income" for s in d["stores"] for t in s["tables"]}
    others = {t for d in provider_descriptors() if d["id"] != "society.income" for s in d["stores"] for t in s["tables"]}
    assert owned and all(s.startswith("src.kb.income_distribution_") for s in owned) and not tables & others
    assert ("society-income-distribution", "^1.0.0") in {(p["pack_id"], p.get("range")) for p in plan["source_packs"]}


@pytest.mark.parametrize("selection", [[], ["pip"], ["eu-silc", "oecd-idd"], ["pip", "eu-silc", "oecd-idd"]])
def test_each_source_feature_resolves_independently(selection):
    plan = plan_for(selection)
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert "noesis-knowledge-engine.income_indicator_for_place" in view.tools
    assert set(plan["features"].get("society") or []) == set(selection)


def test_tools_have_declared_scopes_mutability_and_are_in_the_catalog():
    for name in INCOME_TOOLS:
        assert _mutability(name) == ("write" if name in INCOME_WRITES else "read"), name
    assert _required_scopes("knowledge_engine_mcp", "read", "income_source_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "review_income_identity") == ["knowledge:income:review"]
    assert _required_scopes("knowledge_engine_mcp", "read", "income_indicator_for_place") == ["knowledge:income:read"]
    catalog = {t["id"] for t in json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())["tools"]}
    assert {f"noesis-knowledge-engine.{t}" for t in INCOME_TOOLS} <= catalog
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "`society.income`" in doc and "`noesis-income-distribution-record-v2`" in doc


def test_feature_selection_follows_the_composition_plan_and_readiness_reports_it():
    conn, coordinator, bundles, _ = _migrated()
    assert enabled_providers(conn) == {"pip", "eurostat-silc", "oecd-idd"}
    coordinator.select("society", bundles["society"]["version"], features=["pip"])
    assert coordinator.activate("society-pip-only")["status"] == "published"
    assert enabled_providers(conn) == {"pip"}
    status = readiness(conn, h.NS, scopes={"operator", "knowledge:income:read"})
    assert status["providers"]["oecd-idd"]["status"] == "feature-disabled"
    assert status["providers"]["pip"]["status"] == "unavailable"
    assert status["optional_links"]["economics.labour"] == "provider_absent"
    assert {p["live_verification"]["status"] for p in status["providers"].values()} == {"unverified-live"}


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("society-mcp") / "society.duckdb")
    conn = h.connection()
    h.load_all(conn, revisions=True)
    h.register_places(conn, keys=("de",))
    conn.execute(f"ATTACH '{path}' AS target")
    conn.execute("COPY FROM DATABASE memory TO target")
    conn.close()
    return path


@pytest.fixture
def mcp_env(database, monkeypatch):
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(database, read_only=read_only))
    return tool_map(server.mcp), state, database


def test_answers_identity_and_cited_export_through_mcp(mcp_env):
    tools, state, _ = mcp_env
    assert INCOME_TOOLS <= set(tools)
    status = tools["society_bundle_status"].fn(namespace=h.NS)
    assert {p["status"] for p in status["providers"].values()} == {"fixture-only"}
    proposed = tools["propose_income_place_matches"].fn(namespace=h.NS)
    state.update(principal="bob")
    for assertion in proposed["proposed"]:
        assert tools["review_income_identity"].fn(namespace=h.NS, assertion_id=assertion["assertion_id"],
                                                  decision="accept", reason="identifier")["state"] == "accepted"
    state.update(principal="alice")
    links = tools["link_income_series"].fn(namespace=h.NS)
    assert links["denominators"]["missing"] and links["labour"]["missing"]  # optional links degrade, reported
    answer = tools["income_indicator_for_place"].fn(namespace=h.NS, concept="gini",
                                                    area={"scheme": "eurostat-geo", "code": "DE"}, as_of="2099-07-01")
    assert {r["provider"] for r in answer["results"]} == {"pip", "eurostat-silc", "oecd-idd"}
    assert answer["exclusions"] and answer["minimisation"].startswith("aggregate statistics only")
    bundle = tools["export_income_profile"].fn(namespace=h.NS, area={"scheme": "eurostat-geo", "code": "DE"},
                                               as_of="2099-07-01")
    assert not verify_bundle(bundle).errors
    cited = [o["payload"]["citation"] for o in bundle["objects"]
             if o["payload"].get("kind") == "income-series-vintage"]
    assert cited and all(c["source"] and c["record_revision"] and c["as_of"] for c in cited)
    assert "household" not in json.dumps(cited)
    contracts = tools["income_source_contracts"].fn()
    assert contracts["minimisation"]["excluded"].startswith("EU-SILC user database")


def test_scopes_and_disabling_are_enforced(mcp_env):
    tools, state, path = mcp_env
    state["scopes"] = {"knowledge:income:read", f"namespace:{h.NS}:read"}
    denied = tools["propose_income_place_matches"].fn(namespace=h.NS)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = set(h.SCOPES) | {"operator"}
    assert tools["set_society_bundle_enabled"].fn(namespace=h.NS, enabled=False)["enabled"] is False
    try:
        blocked = tools["income_profile"].fn(namespace=h.NS, area={"scheme": "eurostat-geo", "code": "DE"})
        assert blocked["ok"] is False and blocked["error"]["code"] == "bundle_disabled"
        conn = duckdb.connect(path)
        with pytest.raises(BundleError):
            require_enabled(conn, h.NS)
        require_enabled(conn, "other-namespace")
        conn.close()
    finally:
        assert tools["set_society_bundle_enabled"].fn(namespace=h.NS, enabled=True)["enabled"] is True
