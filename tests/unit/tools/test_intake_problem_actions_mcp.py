"""Problem-action MCP tools expose preview and unavailable execution clearly."""

import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import intake, server


def test_problem_action_mcp_scopes_and_truthful_unavailable_result(tmp_path, monkeypatch):
    path = str(tmp_path / "problem-action-mcp.duckdb")
    scopes = {
        "knowledge:intake:read",
        "knowledge:intake:write",
        "namespace:research:read",
        "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    monkeypatch.setattr(intake, "PROBLEM_ACTION_ADAPTERS", {})

    tools = asyncio.run(server.mcp.get_tools())
    write_tools = (
        "propose_problem_action",
        "consent_problem_action",
        "execute_problem_action",
    )
    for name in (*write_tools, "preview_problem_action"):
        assert name in tools
        assert _required_scopes("knowledge_engine_mcp", "write" if name in write_tools else "read", name) == [
            "knowledge:intake:write" if name in write_tools else "knowledge:intake:read",
        ]
    assert all(_mutability(name) == "write" for name in write_tools)
    assert _mutability("preview_problem_action") == "read"
    assert "noesis-problem-action-v1" in server.knowledge_engine_capabilities.fn()["contracts"]

    opened = tools["start_problem_session"].fn(
        namespace="research",
        request_key="server-incident",
        symptom="The index worker is stuck",
        environment="Desktop 2.7",
        urgency="Blocking",
        success_check="A new record appears within a minute",
        plugin_links=[{
            "workspace_id": "personal", "account_id": "alice",
            "plugin_id": "evidence-reproducibility", "collection": "tasks",
            "record_id": "task-mcp", "authoritative_version": 1,
            "representation": "linked_projection", "authority": "modulo",
        }],
    )
    proposed = tools["propose_problem_action"].fn(
        namespace="research",
        session_id=opened["session_id"],
        command_key="propose",
        expected_revision=1,
        action="service.restart",
        parameters={"service": "search-index", "reason": "Clear a stuck update"},
        rationale="Restart after confirming the worker is idle",
    )
    preview = tools["preview_problem_action"].fn(
        namespace="research", session_id=opened["session_id"],
    )
    assert preview["status"] == "adapter_unavailable"
    assert preview["adapter_configured"] is False
    assert preview["will_execute"] is False
    consented = tools["consent_problem_action"].fn(
        namespace="research",
        session_id=opened["session_id"],
        command_key="consent",
        expected_revision=proposed["revision"],
        proposal_id=proposed["data"]["problem_action_proposal"]["proposal_id"],
        consent=True,
    )
    unavailable = tools["execute_problem_action"].fn(
        namespace="research",
        session_id=opened["session_id"],
        expected_revision=consented["revision"],
        idempotency_key="server-action-1",
        correlation_id="trace-server-action-1",
    )
    assert unavailable["status"] == "unavailable"
    assert unavailable["reason"] == "adapter_unavailable"
    assert unavailable["executed"] is False
    assert unavailable["proposal_id"] == proposed["data"]["problem_action_proposal"]["proposal_id"]
