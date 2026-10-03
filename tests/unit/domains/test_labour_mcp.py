"""Labour MCP entry points: catalog registration, declared scopes, exclusions and answers (#2487)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.labour_statistics import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import labour_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.labour import LABOUR_SCOPES, LABOUR_TOOLS, LABOUR_WRITES


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "labour-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert LABOUR_TOOLS <= set(tools) and set(LABOUR_SCOPES) == LABOUR_TOOLS
    for name in LABOUR_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in LABOUR_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == LABOUR_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in LABOUR_TOOLS:
        assert by_name[name]["required_scopes"] == LABOUR_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/economics/providers/economics.labour.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in LABOUR_TOOLS and name not in LABOUR_WRITES
        assert op["required_scopes"] == LABOUR_SCOPES[name]
    for name in ("labour_indicators_for_place", "labour_series_history", "labour_comparability_notes",
                 "labour_source_contracts", "link_labour_citations"):
        description = tools[name].description.lower()
        assert "no nowcasting" in description and "forecasts" in description and "re-harmonisation" in description


def test_reads_work_with_exactly_the_declared_scopes_and_writes_are_scoped(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    answer = tools["labour_indicators_for_place"].fn(namespace="global",
                                                     place={"scheme": "iso3166-1-alpha3", "code": "DEU"},
                                                     concept="unemployment_rate")
    assert answer["status"] == "reported" and forbidden_keys(answer) == []
    assert "labour-market forecasts" in answer["exclusions"]
    series = tools["list_labour_series"].fn(namespace="global", provider="ilostat")["series"]
    history = tools["labour_series_history"].fn(namespace="global", series_id=series[0]["series_id"])
    assert history["vintages"]
    assert tools["labour_comparability_notes"].fn(namespace="global")["notes"]
    assert tools["list_labour_identity_assertions"].fn(namespace="global")["assertions"] == []
    refused = tools["propose_labour_classification_matches"].fn(namespace="global", kind="sector",
                                                                 target={"scheme": "ISIC", "version": "Rev.4"})
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:labour:write", "namespace:global:write"}
    proposed = tools["propose_labour_classification_matches"].fn(namespace="global", kind="sector",
                                                                  target={"scheme": "ISIC", "version": "Rev.4"})
    assert proposed["assertions"]
    denied = tools["link_labour_citations"].fn(namespace="global")
    assert denied["ok"] is False
    state["scopes"] = set()
    contracts = tools["labour_source_contracts"].fn()
    assert contracts["live_verification"]["bls"]["status"] == "unverified-live"
