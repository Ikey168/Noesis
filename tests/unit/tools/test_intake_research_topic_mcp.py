"""The paired research topic is discoverable and access checked over MCP."""

import asyncio

import duckdb

from src.mcp_host.catalog import _required_scopes
from tools.knowledge_engine_mcp import server


def test_research_topic_mcp_start_replay_and_scope(tmp_path, monkeypatch):
    path = str(tmp_path / "research-topic-mcp.duckdb")
    duckdb.connect(path).close()
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "knowledge:projects:read", "knowledge:projects:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    assert "start_intake_research_topic" in tools
    assert _required_scopes("knowledge_engine_mcp", "write", "start_intake_research_topic") == [
        "knowledge:intake:write", "knowledge:projects:write",
    ]
    request = {
        "namespace": "research", "request_key": "topic-one",
        "questions": ["Why is indexing delayed?"],
        "success_criteria": ["Identify supported and unresolved causes"],
        "scope": {"domains": [], "namespaces": ["research"]},
        "budget": {"requests": 5, "tokens": 10000, "usd_micros": 0},
    }
    first = tools["start_intake_research_topic"].fn(**request)
    assert first["session"]["inputs"]["research_project_id"] == first["project"]["project_id"]
    assert tools["start_intake_research_topic"].fn(**request)["idempotent"]
    scopes.remove("knowledge:projects:write")
    denied = tools["start_intake_research_topic"].fn(**{**request, "request_key": "topic-two"})
    assert denied["error"]["code"] == "unauthorized"
