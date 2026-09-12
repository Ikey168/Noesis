import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_intake_public_tools_and_access(tmp_path, monkeypatch):
    path = str(tmp_path / "intake.duckdb")
    scopes = {
        "knowledge:intake:read",
        "knowledge:intake:write",
        "namespace:research:read",
        "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    assert len(tools["discover_intake_modes"].fn()["modes"]) == 10
    assert (
        tools["route_intake_mode"].fn(answers={"decision_needed": True})["mode"]
        == "Decision Support"
    )
    created = tools["start_intake_mode"].fn(
        namespace="research",
        mode="Exploration",
        request_key="curiosity",
        intent="Browse",
        inputs={},
    )
    identity = {"namespace": "research", "session_id": created["session_id"]}
    assert tools["inspect_intake_mode"].fn(**identity)["revision"] == 1
    assert (
        tools["list_intake_modes"].fn(namespace="research")["sessions"][0]["session_id"]
        == created["session_id"]
    )
    assert (
        tools["command_intake_mode"].fn(
            **identity,
            command_key="end",
            expected_revision=1,
            action="record",
            payload={"data": {"escalation_reason": "Research this"}},
        )["revision"]
        == 2
    )
    assert (
        tools["command_intake_mode"].fn(
            **identity, command_key="done", expected_revision=2, action="complete"
        )["status"]
        == "completed"
    )
    assert len(tools["export_intake_mode"].fn(**identity)["revisions"]) == 3
    assert tools["verify_intake_mode_export"].fn(
        bundle=tools["export_intake_mode"].fn(**identity)
    )["valid"]
    assert (
        tools["export_modulo_intake_handoff"].fn(**identity)["session"]["id"]
        == created["session_id"]
    )
    scopes.remove("namespace:research:read")
    assert (
        tools["inspect_intake_mode"].fn(**identity)["error"]["code"] == "unauthorized"
    )
    assert _mutability("command_intake_mode") == "write"
    assert _required_scopes("knowledge_engine_mcp", "write", "command_intake_mode") == [
        "knowledge:intake:write"
    ]
    assert _required_scopes("knowledge_engine_mcp", "read", "inspect_intake_mode") == [
        "knowledge:intake:read"
    ]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "export_modulo_intake_handoff"
    ) == ["knowledge:intake:read"]
