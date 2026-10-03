"""Trade MCP entry points: catalog registration, declared scopes, exclusions and read-only answers (#2554)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.trade_flows import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import trade_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.trade import TRADE_SCOPES, TRADE_TOOLS, TRADE_WRITES


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "trade-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert TRADE_TOOLS <= set(tools) and set(TRADE_SCOPES) == TRADE_TOOLS
    for name in TRADE_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in TRADE_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == TRADE_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in TRADE_TOOLS:
        assert by_name[name]["required_scopes"] == TRADE_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/economics/providers/economics.trade.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in TRADE_TOOLS and name not in TRADE_WRITES
        assert op["required_scopes"] == TRADE_SCOPES[name]
    # Every answering tool declares the exclusions in its description.
    for name in ("query_trade_flows", "compare_trade_mirror", "query_sanctioned_trade_flows",
                 "export_trade_evidence_bundle", "trade_source_contracts", "link_trade_sanctions"):
        description = tools[name].description.lower()
        assert "no estimation" in description and "no sanctions-evasion inference" in description, name


def test_reads_work_with_exactly_the_declared_scopes_and_writes_are_scoped(mcp_env):
    tools, state = mcp_env
    state["scopes"] = {"knowledge:trade:read", "namespace:global:read"}
    flows = tools["query_trade_flows"].fn(namespace="global", reporter="276", partner="156",
                                          product={"code": "293090", "scheme": "HS", "vintage": "HS2022"})
    assert flows["status"] == "reported" and forbidden_keys(flows) == []
    assert "reconciling reporter and mirror figures into one value" in flows["exclusions"]
    mirror = tools["compare_trade_mirror"].fn(namespace="global", reporter="DE", partner="FR",
                                              product={"code": "85414300", "scheme": "CN", "vintage": "CN2099"})
    assert mirror["comparisons"][0]["asymmetry"]["status"] == "displayed"
    bundle = tools["export_trade_evidence_bundle"].fn(namespace="global", reporter="DE", partner="FR")
    assert bundle["bundle"]["contract"] == "noesis-evidence-bundle-v1"
    mapped = tools["map_trade_product_code"].fn(namespace="global", code="854143",
                                                source={"scheme": "HS", "vintage": "HS2022"},
                                                target={"scheme": "HS", "vintage": "HS2017"})
    assert mapped["exact"] is False
    assert len(tools["list_trade_concordances"].fn(namespace="global")["concordances"]) == 2
    assert tools["list_trade_links"].fn(namespace="global")["links"] == []
    assert tools["list_trade_identity_assertions"].fn(namespace="global")["assertions"] == []
    denied = tools["query_sanctioned_trade_flows"].fn(namespace="global", reporter="276", partner="156")
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] |= {"knowledge:legal:read"}
    absent = tools["query_sanctioned_trade_flows"].fn(namespace="global", reporter="276", partner="156")
    assert absent["status"] == "provider_absent"
    refused = tools["propose_trade_product_matches"].fn(namespace="global", target={"scheme": "HS",
                                                                                   "vintage": "HS2022"})
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:trade:write", "namespace:global:write"}
    proposed = tools["propose_trade_product_matches"].fn(namespace="global", target={"scheme": "HS",
                                                                                    "vintage": "HS2022"})
    assert proposed["assertions"]
    state["scopes"] = set()
    contracts = tools["trade_source_contracts"].fn()
    assert contracts["live_verification"]["un-comtrade"]["status"] == "unverified-live"
