"""Typed Iteration tools are discoverable and enforce intake/namespace scopes."""

import asyncio

import duckdb

from src.mcp_host.catalog import _required_scopes
from tools.knowledge_engine_mcp import server


def test_iteration_mcp_discovery_and_scope(tmp_path, monkeypatch):
    path = str(tmp_path / "iteration-mcp.duckdb")
    scopes = {"knowledge:intake:read", "knowledge:intake:write",
              "namespace:research:read", "namespace:research:write"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(server, "_connection",
                        lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    for name in ("start_intake_iteration", "record_intake_iteration_outcome",
                 "propose_intake_playbook_revision", "accept_intake_playbook_revision",
                 "review_intake_iteration_stability"):
        assert name in tools
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:intake:write",
        ]
    scopes.remove("knowledge:intake:write")
    denied = tools["start_intake_iteration"].fn(
        namespace="research", request_key="cycle", playbook_id="playbook:missing",
        expected_revision=1, expected="Faster search",
        stability_criteria="Three clean runs", intent="Check the repair",
    )
    assert denied["error"]["code"] == "unauthorized"
