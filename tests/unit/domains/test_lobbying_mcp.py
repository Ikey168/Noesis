"""Lobbying MCP entry points: catalog registration, declared scopes, read-only answers and reviews (#1993, #2009)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.lobbying import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import lobbying_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.lobbying import (
    LOBBYING_SCOPES,
    LOBBYING_TOOLS,
    LOBBYING_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "lobbying-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    dossiers = h.dossiers(conn)
    conn.close()
    scopes = set(h.REVIEW_SCOPES | dossiers["scopes"] | {"knowledge:reports:write"})
    state = {"principal": "alice", "scopes": scopes}
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    return asyncio.run(server.mcp.get_tools()), state, dossiers


def test_tools_are_registered_with_every_scope_they_read_and_write(mcp_env):
    tools, _, _ = mcp_env
    assert LOBBYING_TOOLS <= set(tools)
    for name in LOBBYING_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in LOBBYING_WRITES else "read"), name
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == LOBBYING_SCOPES[name]
        )
    assert set(LOBBYING_SCOPES) == LOBBYING_TOOLS
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    names = {t["name"] for t in catalog["tools"]}
    assert LOBBYING_TOOLS <= names
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in ("legislative_dossier_timeline", "legislative_dossier_dependencies"):
        assert "knowledge:political:lobbying:read" in by_name[name]["required_scopes"]
    descriptor = json.loads(
        (h.ROOT / "packs/political/providers/political.lobbying.json").read_text()
    )
    assert {
        op["tool"].split(".", 1)[1] for op in descriptor["operations"]
    } <= LOBBYING_TOOLS
    for op in descriptor["operations"]:
        assert op["required_scopes"] == LOBBYING_SCOPES[op["tool"].split(".", 1)[1]]
    assert (
        "no influence claim"
        in tools["list_dossier_declared_interests"].description.lower()
    )


def test_answers_links_and_reviews_through_mcp(mcp_env):
    tools, state, dossiers = mcp_env
    dossier_id = dossiers["eu"]["dossier_id"]
    linked = tools["link_lobbying_dossier"].fn(
        namespace="global", dossier_namespace=h.DOSSIER_NS, dossier_id=dossier_id
    )
    assert len(linked["linked"]) == 4, linked
    answer = tools["list_dossier_declared_interests"].fn(
        namespace="global", dossier_namespace=h.DOSSIER_NS, dossier_id=dossier_id
    )
    assert (
        answer["declared_interests"][0]["native_id"] == "000000000101-01"
        and forbidden_keys(answer) == []
    )
    timeline = tools["legislative_dossier_timeline"].fn(
        namespace=h.DOSSIER_NS, dossier_id=dossier_id, lobbying_namespace="global"
    )
    assert len(timeline["lobbying_entries"]) == 4
    meetings = tools["list_official_meetings"].fn(
        namespace="global", official_id="ep-mep:990001"
    )
    assert len(meetings["meetings"]) == 2
    history = tools["list_registrant_declarations"].fn(
        namespace="global", register="eu-tr", native_id="000000000303-03"
    )
    assert [r["change"] for r in history["registrants"][0]["history"]] == [
        "registered",
        "deregistered",
    ]
    candidate = next(
        link for link in linked["links"] if link["link_kind"] == "unreviewed-candidate"
    )
    state["principal"] = "bob"
    reviewed = tools["review_lobbying_dossier_link"].fn(
        namespace="global",
        link_id=candidate["link_id"],
        decision="accept",
        reason="agenda names the file",
    )
    assert (
        reviewed["link_kind"] == "reviewed-assertion" and reviewed["reviewer"] == "bob"
    )
    proposed = tools["propose_lobbying_identity_matches"].fn(namespace="global")
    assert proposed["candidates"] and all(
        c["state"] == "proposed" for c in proposed["candidates"]
    )
    state["scopes"] = set(h.READ_ONLY)
    refused = tools["revert_lobbying_dossier_link"].fn(
        namespace="global", link_id=candidate["link_id"], reason="x"
    )
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    contracts = tools["lobbying_source_contracts"].fn()
    assert (
        contracts["contracts"]["integrity-watch-eu"]["access_decision"]
        == "not-implemented"
    )
