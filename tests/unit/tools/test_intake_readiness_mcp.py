"""Scoped readiness is available through the supported MCP tool surface."""

import asyncio

import duckdb

from src.mcp_host.catalog import _required_scopes
from tools.knowledge_engine_mcp import server


def test_scoped_readiness_mcp_and_denied_namespace(tmp_path, monkeypatch):
    path = str(tmp_path / "readiness.duckdb")
    duckdb.connect(path).close()
    scopes = {"knowledge:intake:read", "namespace:research:read"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(server, "_connection",
                        lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    result = tools["preflight_intake_mode"].fn(namespace="research", mode="Maintenance")
    assert result["contract"] == "noesis-intake-readiness-v1"
    assert result["modes"][0]["mode"] == "Maintenance"
    assert result["modes"][0]["complete_journey_ready"] is False
    assert _required_scopes("knowledge_engine_mcp", "read", "preflight_intake_mode") == [
        "knowledge:intake:read",
    ]
    denied = tools["preflight_intake_mode"].fn(namespace="archive")
    assert denied["error"]["code"] == "unauthorized"
