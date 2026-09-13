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
    project = tools["start_intake_creation"].fn(
        namespace="research", request_key="creation-one", title="Index guide",
        audience="Operators", artifact_type="documentation", purpose="Explain repair",
        criteria=["Usable steps"],
    )
    assert project["project_id"].startswith("creation:")
    assert tools["inspect_intake_creation"].fn(
        namespace="research", project_id=project["project_id"],
    )["status"] == "draft"
    assert _required_scopes("knowledge_engine_mcp", "write",
                            "command_intake_creation") == ["knowledge:intake:write"]
    scopes.remove("namespace:research:read")
    denied = tools["inspect_intake_creation"].fn(
        namespace="research", project_id=project["project_id"],
    )
    assert denied["error"]["code"] == "unauthorized"
