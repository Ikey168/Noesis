"""Sanctions MCP entry points: catalog registration, read-only answers and no screening fields (#1963)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import sanctions_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.sanctions import (
    SANCTIONS_SCOPES,
    SANCTIONS_TOOLS,
    SANCTIONS_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "sanctions-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_legal(conn, "cellar-sanctions-acts-eng")
    h.load_legal(conn, "cellar-dual-use-2021-821")
    for list_id, files in h.FILES.items():
        for name in files:
            h.apply(conn, list_id, name)
    conn.close()
    state = {"principal": "analyst", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_scopes_mutability_and_in_the_catalog(mcp_env):
    tools, _ = mcp_env
    assert SANCTIONS_TOOLS <= set(tools)
    for name in SANCTIONS_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in SANCTIONS_WRITES else "read"), name
        expected = SANCTIONS_SCOPES.get(
            name,
            [
                "knowledge:legal:write"
                if name in SANCTIONS_WRITES
                else "knowledge:legal:read"
            ],
        )
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == expected
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    assert {
        t["name"] for t in catalog["tools"] if t["name"] in SANCTIONS_TOOLS
    } == SANCTIONS_TOOLS
    descriptor = json.loads(
        (h.ROOT / "packs/legal/providers/legal.sanctions.json").read_text()
    )
    assert {
        op["tool"].split(".", 1)[1] for op in descriptor["operations"]
    } <= SANCTIONS_TOOLS - SANCTIONS_WRITES
    for name in ("lookup_sanctions_designation", "designation_history_as_of"):
        assert "no screening result" in tools[name].description.lower()
        assert "compliance status" in tools[name].description.lower()


def test_as_of_answers_run_on_a_read_only_connection(mcp_env):
    tools, _ = mcp_env
    answer = tools["designation_history_as_of"].fn(
        namespace="global", as_of="2026-03-01", identifier="IMO 9999991"
    )
    assert sorted(answer["lists"]) == ["eu", "ofac", "uk"], answer
    assert (
        answer["lists"]["eu"][0]["status"] == "listed"
        and h.forbidden_keys(answer) == []
    )
    control = tools["control_list_entry_as_of"].fn(
        namespace="global", control_code="1C350", as_of="2026-01-01"
    )
    assert control["status"] == "entry_in_edition"
    looked = tools["lookup_sanctions_designation"].fn(
        namespace="global", list_id="uk", list_entry_id="RUS9001"
    )
    assert looked["lists"]["uk"][0]["status"] == "listed"
    contracts = tools["sanctions_source_contracts"].fn()
    assert (
        contracts["contracts"]["eu-sanctions-map"]["access_decision"]
        == "not-implemented"
    )


def test_writes_and_scopes_through_mcp(mcp_env):
    tools, state = mcp_env
    proposed = tools["propose_sanctions_identity_matches"].fn(namespace="global")
    assert len(proposed["proposed"]) >= 3
    candidate = proposed["candidates"][0]["candidate_id"]
    state["principal"] = "reviewer"
    accepted = tools["review_sanctions_identity_match"].fn(
        namespace="global",
        candidate_id=candidate,
        decision="accept",
        reason="identifier on both lists",
    )
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "reviewer"
    state["scopes"] = set(h.READ_ONLY)
    refused = tools["revert_sanctions_identity_match"].fn(
        namespace="global", candidate_id=candidate, reason="x"
    )
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"


def test_propose_works_with_exactly_the_declared_scopes(mcp_env):
    tools, state = mcp_env
    namespace_access = {"namespace:global:read", "namespace:global:write"}
    state["scopes"] = (
        set(SANCTIONS_SCOPES["propose_sanctions_identity_matches"]) | namespace_access
    )
    proposed = tools["propose_sanctions_identity_matches"].fn(
        namespace="global", ownership_namespace="global"
    )
    assert "error" not in proposed and len(proposed["proposed"]) >= 3, proposed
    state["scopes"] = {
        "knowledge:legal:read",
        "knowledge:ownership:write",
    } | namespace_access
    refused = tools["propose_sanctions_identity_matches"].fn(
        namespace="global", ownership_namespace="global"
    )
    assert (
        refused["ok"] is False
    )  # ownership records cannot be read without the declared read scope
