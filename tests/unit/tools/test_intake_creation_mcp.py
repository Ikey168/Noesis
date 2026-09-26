"""Creation project and report adapter are discoverable over supported MCP."""

import asyncio

import duckdb

from src.mcp_host.catalog import _required_scopes
from tools.knowledge_engine_mcp import server


def test_creation_mcp_round_trip_and_denied_read(tmp_path, monkeypatch):
    path = str(tmp_path / "creation-mcp.duckdb")
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "knowledge:reports:read", "knowledge:reports:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    assert "handoff_intake_creation" in tools
    project = tools["start_intake_creation"].fn(
        namespace="research", request_key="creation-one", title="Index guide",
        audience="Operators", artifact_type="documentation", purpose="Explain repair",
        criteria=["Usable steps"],
        plugin_links=[{
            "workspace_id": "personal", "account_id": "alice",
            "plugin_id": "writing-editorial", "collection": "documents",
            "record_id": "doc-mcp", "authoritative_version": 1,
            "representation": "linked_projection", "authority": "modulo",
        }],
    )
    assert project["project_id"].startswith("creation:")
    assert project["plugin_links"][0]["plugin_id"] == "writing-editorial"
    assert tools["inspect_intake_creation"].fn(
        namespace="research", project_id=project["project_id"],
    )["status"] == "draft"
    assert _required_scopes("knowledge_engine_mcp", "write",
                            "command_intake_creation") == ["knowledge:intake:write"]
    assert _required_scopes("knowledge_engine_mcp", "write",
                            "handoff_intake_creation") == ["knowledge:intake:write"]
    assert tools["handoff_intake_creation"].fn(
        namespace="research", project_id=project["project_id"], request_key="teach",
        destination_mode="Internalization", reason="Practice the guide",
    )["error"]["code"] == "not_finished"
    scopes.remove("namespace:research:read")
    denied = tools["inspect_intake_creation"].fn(
        namespace="research", project_id=project["project_id"],
    )
    assert denied["error"]["code"] == "unauthorized"
