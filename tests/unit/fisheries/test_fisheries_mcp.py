"""FI12: the Fisheries MCP entry points, declaration, readiness, optional features and enablement (#2339)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.fisheries_bundle import BUNDLE
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.fisheries import harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.fisheries import FISHERIES_READS, FISHERIES_TOOLS, FISHERIES_WRITES
from src.mcp_host.introspection import tool_map

NS = h.NS


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    directory = tmp_path_factory.mktemp("fisheries-mcp")
    env = h.loaded_env(directory)
    env.conn.close()
    return str(directory / "fisheries.duckdb")


@pytest.fixture
def mcp_env(database, monkeypatch):
    state = {"principal": "alice", "scopes": set(h.ALL)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(database, read_only=read_only))
    return tool_map(server.mcp), state


def test_declared_contributions_and_catalog_scopes():
    declared = {tool for workflow in BUNDLE["contributions"]["workflows"].values() for tool in workflow["tools"]}
    assert declared <= FISHERIES_TOOLS
    assert BUNDLE["contributions"]["source_packs"][0]["pack_id"] == "fisheries-maritime"
    assert set(BUNDLE["contributions"]["optional_features"]) == {"sanctions", "geospatial"}
    assert any("enforcement" in item for item in BUNDLE["never"])
    for name in FISHERIES_TOOLS:
        assert _mutability(name) == ("write" if name in FISHERIES_WRITES else "read"), name
    assert _required_scopes("knowledge_engine_mcp", "read", "fisheries_source_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "review_fisheries_identity") == [
        "knowledge:fisheries:review"]
    assert _required_scopes("knowledge_engine_mcp", "read", "fisheries_vessel_status") == [
        "knowledge:fisheries:read"]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert {t["name"] for t in catalog["tools"] if t["name"] in FISHERIES_TOOLS} == FISHERIES_TOOLS


def test_identity_status_aggregates_and_optional_features_through_mcp(mcp_env):
    tools, state = mcp_env
    status = tools["fisheries_bundle_status"].fn(namespace=NS)
    assert status["entry_points"]["acquisition"] == "fixture-only"
    assert {p["status"] for p in status["providers"].values()} == {"fixture-only"}
    assert {p["live_verification"]["status"] for p in status["providers"].values()} == {"unverified-live"}
    assert status["optional"]["features"]["sanctions"]["status"] == "provider_unavailable"
    assert status["optional"]["citing_providers"]["agrifood.food-systems"]["status"] == "provider_unavailable"
    assert "gfw" in tools["fisheries_source_contracts"].fn()["contracts"]
    proposed = tools["propose_fisheries_identities"].fn(namespace=NS)
    assert any(m["basis"] == "imo" and m["state"] == "accepted" for m in proposed["matches"]), proposed
    linked = tools["link_fisheries_citations"].fn(namespace=NS)
    assert linked["sanctions"]["status"] == "provider_unavailable"  # the optional pack is absent
    assert linked["areas"]["status"] == "provider_unavailable"
    assert linked["agrifood.food-systems"]["status"] == "provider_unavailable"
    projected = tools["project_fisheries_areas"].fn(namespace=NS)
    assert projected["places"], projected
    assert tools["link_fisheries_citations"].fn(namespace=NS, targets=["areas"])["areas"]["linked"]
    vessel = tools["fisheries_vessel_status"].fn(namespace=NS, query=h.IMO_REFLAGGED, as_of="2026-09-20",
                                                 evidence_bundle=True)
    assert vessel["listings"] and vessel["evidence_bundle"]["contract"] == "noesis-evidence-bundle-v1"
    aggregates = tools["fisheries_area_aggregates"].fn(namespace=NS, area="34", period_from="2021-01-01",
                                                       period_to="2022-12-31")
    assert aggregates["catch"] and aggregates["not_published"]
    assert not h.forbidden_keys(vessel) and not h.forbidden_keys(aggregates)
    state["scopes"] = {"knowledge:read"}
    denied = tools["fisheries_vessel_status"].fn(namespace=NS, query=h.IMO_CLEAN)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"


def test_monitor_and_disable_through_mcp(mcp_env):
    tools, state = mcp_env
    created = tools["create_fisheries_monitor"].fn(namespace=NS, request_key="mcp-1", lists=["iccat:iuu-vessels"])
    assert created["subscription_id"].startswith("subscription:"), created
    ran = tools["run_fisheries_monitor"].fn(namespace=NS, subscription_id=created["subscription_id"])
    assert ran["baseline"] and ran["notifications"]
    assert tools["poll_fisheries_monitor"].fn(namespace=NS, subscription_id=created["subscription_id"])["events"]
    state["scopes"] = set(h.ALL) | {"operator"}
    assert tools["set_fisheries_bundle_enabled"].fn(namespace=NS, enabled=False)["enabled"] is False
    blocked = tools["fisheries_vessel_status"].fn(namespace=NS, query=h.IMO_CLEAN)
    assert blocked["error"]["code"] == "bundle_disabled"
    assert "contracts" in tools["fisheries_source_contracts"].fn()
    assert tools["set_fisheries_bundle_enabled"].fn(namespace=NS, enabled=True)["enabled"] is True


def test_every_read_tool_is_read_only_against_an_empty_warehouse(tmp_path, monkeypatch):
    path = str(tmp_path / "empty.duckdb")
    duckdb.connect(path).close()
    monkeypatch.setattr(server, "_context", lambda: ("alice", set(h.ALL)))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = tool_map(server.mcp)
    for name in sorted(FISHERIES_READS - {"fisheries_source_contracts"}):
        properties = tools[name].parameters.get("properties", {})
        kwargs = {key: "missing" for key in tools[name].parameters.get("required", [])}
        kwargs.update({k: NS for k in ("namespace",) if k in properties})
        result = tools[name].fn(**kwargs)
        assert isinstance(result, dict), name
