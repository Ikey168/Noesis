from __future__ import annotations

import asyncio
import inspect

import duckdb

from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_source_identity_mcp_registry_alias_graph_and_dossier(tmp_path, monkeypatch):
    database = tmp_path / "sources.duckdb"
    scopes = {"knowledge:source-identity:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    denied = _call(
        tools["register_source_identity"],
        namespace="political",
        kind="publication",
        display_name="Outlet",
    )
    assert denied["error"]["code"] == "unauthorized"

    scopes.update(
        {"knowledge:source-identity:write", "knowledge:source-identity:review"}
    )
    outlet = _call(
        tools["register_source_identity"],
        namespace="political",
        kind="publication",
        display_name="Outlet",
        native_ids={"registry": "outlet-1"},
    )
    owner = _call(
        tools["register_source_identity"],
        namespace="political",
        kind="organization",
        display_name="Owner",
        native_ids={"registry": "owner-1"},
    )
    alias = _call(
        tools["decide_source_alias"],
        namespace="political",
        source_id=outlet["source_id"],
        alias_type="handle",
        value="@Outlet",
        reason="The verified profile links to the outlet site.",
    )
    assert alias["normalized_alias"] == "@outlet"
    resolved = _call(
        tools["resolve_source_alias"],
        namespace="political",
        alias_type="handle",
        value="outlet",
    )
    assert resolved["matches"][0]["source_id"] == outlet["source_id"]
    relationship = _call(
        tools["add_source_relationship"],
        namespace="political",
        from_source_id=owner["source_id"],
        to_source_id=outlet["source_id"],
        relationship_type="ownership",
        evidence=[{"citation": "registry:1"}],
    )
    dossier = _call(
        tools["source_identity_dossier"],
        namespace="political",
        source_id=outlet["source_id"],
    )
    assert (
        dossier["relationships"][0]["relationship_id"]
        == relationship["relationship_id"]
    )
    independent = _call(
        tools["explain_source_independence"],
        namespace="political",
        source_ids=[outlet["source_id"], owner["source_id"]],
    )
    assert len(independent["groups"]) == 1
    assert (
        _call(
            tools["lookup_source_identity"],
            namespace="osint",
            source_id=outlet["source_id"],
        )
        is None
    )
