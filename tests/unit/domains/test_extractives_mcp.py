"""Extractives MCP entry points: catalog registration, declared scopes, exclusions, minimisation (#2709)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.ingestion.extractives_sources import personal_keys
from src.kb.extractives_records import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import extractives_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.extractives import (
    EXTRACTIVES_SCOPES,
    EXTRACTIVES_TOOLS,
    EXTRACTIVES_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "extractives-mcp.duckdb")
    conn = duckdb.connect(path)
    h.reviewed(conn, revisions=True)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_with_their_declared_scopes_and_exclusions(mcp_env):
    tools, _ = mcp_env
    assert EXTRACTIVES_TOOLS <= set(tools) and set(EXTRACTIVES_SCOPES) == EXTRACTIVES_TOOLS
    for name in EXTRACTIVES_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in EXTRACTIVES_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == EXTRACTIVES_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in EXTRACTIVES_TOOLS:
        assert by_name[name]["required_scopes"] == EXTRACTIVES_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/economics/providers/economics.extractives.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in EXTRACTIVES_TOOLS and name not in EXTRACTIVES_WRITES
        assert op["required_scopes"] == EXTRACTIVES_SCOPES[name]
    for name in ("query_extractives_production", "query_extractives_company_payments",
                 "query_extractives_country_payments", "export_extractives_evidence_bundle",
                 "extractives_source_contracts"):
        description = tools[name].description.lower()
        assert "no own reserve estimates" in description and "no price forecasts" in description, name


def test_reads_work_with_the_declared_scopes_declare_exclusions_and_return_no_personal_fields(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    country = tools["query_extractives_country_payments"].fn(namespace="global", country="NL")
    assert country["status"] == "answered" and forbidden_keys(country) == [] and personal_keys(country) == []
    assert "Fixture-Person" not in json.dumps(country) and "msg@example.org" not in json.dumps(country)
    assert "price forecasts" in country["exclusions"]
    production = tools["query_extractives_production"].fn(namespace="global", commodity="copper", country="CL")
    assert {s["provider"] for s in production["sources"]} == {"bgs-wms", "usgs-mcs"}
    bundle = tools["export_extractives_evidence_bundle"].fn(namespace="global", query="country", key="NL")
    for assertion in bundle["evidence_bundle"]["sections"][0]["assertions"]:
        dependency = assertion["dependencies"][0]
        assert dependency["revision"] and dependency["as_of"] and assertion["citations"]
    refused = tools["query_extractives_company_payments"].fn(namespace="global", entity=h.INT_ENTITY,
                                                             ownership_namespace=h.OWN_NS)
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = set(h.READ_ONLY) | {"knowledge:ownership:read", f"namespace:{h.OWN_NS}:read"}
    company = tools["query_extractives_company_payments"].fn(namespace="global", entity=h.INT_ENTITY,
                                                             ownership_namespace=h.OWN_NS)
    assert company["status"] == "answered"
    denied = tools["propose_extractives_commodity_matches"].fn(namespace="global")
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = set()
    contracts = tools["extractives_source_contracts"].fn()
    assert contracts["live_verification"]["eiti"]["status"] == "unverified-live"
    assert contracts["coverage"]["minimisation"]["excluded"]
