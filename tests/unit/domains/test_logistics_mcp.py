"""Logistics MCP entry points: catalog registration, declared scopes, exclusions and read-only answers (#2549)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.logistics_records import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import logistics_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.logistics import LOGISTICS_SCOPES, LOGISTICS_TOOLS, LOGISTICS_WRITES


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "logistics-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_their_declared_scopes_and_exclusions(mcp_env):
    tools, _ = mcp_env
    assert LOGISTICS_TOOLS <= set(tools) and set(LOGISTICS_SCOPES) == LOGISTICS_TOOLS
    for name in LOGISTICS_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in LOGISTICS_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == LOGISTICS_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in LOGISTICS_TOOLS:
        assert by_name[name]["required_scopes"] == LOGISTICS_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/economics/providers/economics.logistics.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in LOGISTICS_TOOLS and name not in LOGISTICS_WRITES
        assert op["required_scopes"] == LOGISTICS_SCOPES[name]
    for name in ("query_port_logistics", "query_country_logistics", "query_route_logistics",
                 "logistics_freight_indices", "logistics_source_contracts"):
        assert "no freight-rate forecasting" in tools[name].description.lower(), name


def test_reads_work_with_exactly_the_declared_scopes_and_writes_are_scoped(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    port = tools["query_port_logistics"].fn(namespace="global", port="DEHAM")
    assert port["status"] == "answered" and forbidden_keys(port) == []
    assert "freight-rate forecasting or rate predictions" in port["exclusions"]
    assert tools["query_route_logistics"].fn(namespace="global", origin="DEHAM",
                                             destination="DEBRV")["status"] == "none_on_record"
    assert len(tools["list_logistics_ports"].fn(namespace="global")["ports"]) == 5
    refused = tools["propose_logistics_port_matches"].fn(namespace="global")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:logistics:write", "namespace:global:write"}
    proposed = tools["propose_logistics_port_matches"].fn(namespace="global")
    assert proposed["exact"]
    state["scopes"] = set()
    contracts = tools["logistics_source_contracts"].fn()
    assert contracts["live_verification"]["unctadstat"]["status"] == "unverified-live"
