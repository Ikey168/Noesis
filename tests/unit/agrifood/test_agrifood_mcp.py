"""AF11: the Agriculture and Food Systems MCP entry points, declaration, readiness and enablement (#2365)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.agrifood_bundle import BUNDLE
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.agrifood import harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.agrifood import AGRIFOOD_READS, AGRIFOOD_TOOLS, AGRIFOOD_WRITES

NS = h.NS


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    directory = tmp_path_factory.mktemp("agrifood-mcp")
    env = h.loaded_env(directory)
    env.seed_rasff()
    env.conn.close()
    return str(directory / "agrifood.duckdb")


@pytest.fixture
def mcp_env(database, monkeypatch):
    state = {"principal": "alice", "scopes": set(h.ALL) | {"knowledge:ingestion:execute"}}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(database, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_declared_contributions_and_catalog_scopes():
    declared = {tool for workflow in BUNDLE["contributions"]["workflows"].values() for tool in workflow["tools"]}
    assert declared <= AGRIFOOD_TOOLS
    assert BUNDLE["contributions"]["source_packs"][0]["pack_id"] == "agrifood"
    assert any("forecast" in item for item in BUNDLE["never"])
    for name in AGRIFOOD_TOOLS:
        assert _mutability(name) == ("write" if name in AGRIFOOD_WRITES else "read"), name
    assert _required_scopes("knowledge_engine_mcp", "read", "agrifood_source_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "review_agrifood_crosswalk") == [
        "knowledge:agrifood:review"]
    assert _required_scopes("knowledge_engine_mcp", "read", "agrifood_series_as_of") == ["knowledge:agrifood:read"]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert {t["name"] for t in catalog["tools"] if t["name"] in AGRIFOOD_TOOLS} == AGRIFOOD_TOOLS
    assert AGRIFOOD_READS & AGRIFOOD_WRITES == set()


def test_series_crosswalks_links_and_monitors_through_mcp(mcp_env):
    tools, state = mcp_env
    status = tools["agrifood_bundle_status"].fn(namespace=NS)
    assert status["entry_points"]["acquisition"] == "fixture-only"
    assert {p["status"] for p in status["providers"].values()} == {"fixture-only"}
    contracts = tools["agrifood_source_contracts"].fn()
    assert set(contracts["contracts"]) == {"faostat", "nass-quickstats", "fas-psd", "eurostat-agri",
                                           "agri-food-portal"}
    assert contracts["flag_vocabularies"]["nass-value-codes"]["(D)"]["classes"] == ["withheld"]
    assert tools["register_agrifood_places"].fn(namespace=NS)["places"]
    proposed = tools["propose_agrifood_crosswalks"].fn(namespace=NS)["crosswalks"]
    corn = next(c for c in proposed if {c["left"]["scheme"], c["right"]["scheme"]} == {"faostat-item",
                                                                                      "nass-commodity"})
    reviewed = tools["review_agrifood_crosswalk"].fn(namespace=NS, crosswalk_id=corn["crosswalk_id"],
                                                     decision="accept", reason="same crop")
    assert reviewed["state"] == "accepted"
    resolved = tools["resolve_agrifood_commodity"].fn(namespace=NS, query="nass-commodity:CORN")
    assert {"faostat-item", "nass-commodity"} <= {c["scheme"] for c in resolved["codes"]}
    series = tools["agrifood_series_as_of"].fn(namespace=NS, commodity="corn", place="United States",
                                               as_of="2025-06-30")
    assert series["status"] == "found" and {"faostat", "nass-quickstats"} <= set(series["sources"])
    history = tools["agrifood_revision_history"].fn(namespace=NS, series_id=series["series"][0]["series_id"])
    assert history["vintages"]
    linked = tools["link_agrifood_citations"].fn(namespace=NS)
    assert linked["rasff"]["linked"] and linked["trade"]["status"] == "provider_unavailable"
    assert tools["list_agrifood_links"].fn(namespace=NS, owner="products-rasff")["links"]
    monitor = tools["create_agrifood_monitor"].fn(namespace=NS, request_key="corn-us", commodity="corn",
                                                  place="United States")
    run = tools["run_agrifood_monitor"].fn(namespace=NS, subscription_id=monitor["subscription_id"])
    assert run["baseline"] and {n["kind"] for n in run["notifications"]} >= {"release", "linked_alert"}
    none = tools["agrifood_series_as_of"].fn(namespace=NS, commodity="soya beans", place="Germany")
    assert none["status"] == "none_on_record"
    state["scopes"] = {"knowledge:agrifood:read", f"namespace:{NS}:read"}
    denied = tools["review_agrifood_crosswalk"].fn(namespace=NS, crosswalk_id=corn["crosswalk_id"],
                                                   decision="reject", reason="no")
    assert "error" in json.dumps(denied).lower() or denied.get("status") == "error"
