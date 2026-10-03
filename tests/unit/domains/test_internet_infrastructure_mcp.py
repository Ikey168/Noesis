"""Internet infrastructure MCP entry points: catalog registration, declared scopes, exclusions and refusals (II11)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.internet_infrastructure_records import forbidden_paths, personal_data_paths
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import internet_infrastructure_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.internet_infrastructure import (
    INTERNET_INFRASTRUCTURE_SCOPES,
    INTERNET_INFRASTRUCTURE_TOOLS,
    INTERNET_INFRASTRUCTURE_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "internet-infrastructure-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_their_declared_scopes_and_exclusions(mcp_env):
    tools, _ = mcp_env
    assert INTERNET_INFRASTRUCTURE_TOOLS <= set(tools)
    assert set(INTERNET_INFRASTRUCTURE_SCOPES) == INTERNET_INFRASTRUCTURE_TOOLS
    for name in INTERNET_INFRASTRUCTURE_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in INTERNET_INFRASTRUCTURE_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == INTERNET_INFRASTRUCTURE_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in INTERNET_INFRASTRUCTURE_TOOLS:
        assert by_name[name]["required_scopes"] == INTERNET_INFRASTRUCTURE_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/technology/providers/technology.internet-infrastructure.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in INTERNET_INFRASTRUCTURE_TOOLS and name not in INTERNET_INFRASTRUCTURE_WRITES
        assert op["required_scopes"] == INTERNET_INFRASTRUCTURE_SCOPES[name]
    scopes = {s for v in INTERNET_INFRASTRUCTURE_SCOPES.values() for s in v}
    assert {"knowledge:technical:internet-infrastructure:read", "knowledge:technical:internet-infrastructure:write",
            "knowledge:technical:internet-infrastructure:review"} <= scopes
    for name in ("internet_infrastructure_records_as_of", "internet_infrastructure_history",
                 "export_internet_infrastructure_bundle", "internet_infrastructure_source_contracts",
                 "link_internet_infrastructure_osint"):
        description = " ".join(tools[name].description.lower().split())
        assert "no ip-keyed or person-keyed lookups" in description and "no merged records" in description, name


def test_reads_answer_declared_resources_and_refuse_ip_person_and_wildcard_queries(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    answer = tools["internet_infrastructure_records_as_of"].fn(namespace="global", resource="AS64500",
                                                               as_of="2097-12-31")
    assert answer["status"] == "answered" and answer["osint_review_gate"].startswith("not gated")
    assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
    assert "ranking of networks" in answer["exclusions"]
    for resource, code in (("192.0.2.1", "ip_lookup_refused"), ("hostmaster@example.org",
                                                                 "person_identifier_refused"),
                           ("*.example.org", "wildcard_refused"), ("AS64501", "undeclared_resource")):
        refused = tools["internet_infrastructure_records_as_of"].fn(namespace="global", resource=resource)
        assert refused["ok"] is False and refused["error"]["code"] == code, resource
    bundle = tools["export_internet_infrastructure_bundle"].fn(namespace="global", resource="example.org",
                                                               as_of="2096-01-01")
    assert bundle["bundle"]["objects"]
    declared = tools["list_internet_infrastructure_resources"].fn(namespace="global")["declared"]
    assert {(d["kind"], d["value"]) for d in declared} >= {("asn", "AS64500"), ("prefix", "192.0.2.0/24"),
                                                          ("domain", "example.org")}
    denied = tools["propose_internet_infrastructure_identity"].fn(namespace="global")
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:technical:internet-infrastructure:write", "namespace:global:write"}
    proposed = tools["propose_internet_infrastructure_identity"].fn(namespace="global")
    assert proposed["proposed"] and {a["state"] for a in proposed["proposed"]} == {"proposed"}
    osint = tools["link_internet_infrastructure_osint"].fn(namespace="global")
    assert osint["ok"] is False  # the source-identity read scope is declared and required
    state["scopes"] |= {"knowledge:source-identity:read"}
    linked = tools["link_internet_infrastructure_osint"].fn(namespace="global")
    assert {lk["state"] for lk in linked["links"]} == {"provider_absent"}
    state["scopes"] = set()
    contracts = tools["internet_infrastructure_source_contracts"].fn()
    assert contracts["live_verification"]["ripestat"]["status"] == "unverified-live"
    assert contracts["contracts"]["caida"]["status"] == "not-implemented"
