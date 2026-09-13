"""Maintenance review is discoverable and scoped on the supported MCP surface."""

import asyncio

import duckdb

from src.mcp_host.catalog import _required_scopes
from tools.knowledge_engine_mcp import server


def test_maintenance_mcp_review_and_denied_read(tmp_path, monkeypatch):
    path = str(tmp_path / "maintenance-mcp.duckdb")
    duckdb.connect(path).close()
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    scan = tools["scan_intake_maintenance"].fn(namespace="research")
    assert scan["contract"] == "noesis-intake-maintenance-review-v1"
    assert [item["reason"] for item in scan["findings"]] == ["routine_health_check"]
    started = tools["start_intake_maintenance"].fn(
        namespace="research", request_key="monthly", intent="Check current health",
    )
    assert started["mode"] == "Maintenance"
    finding = scan["findings"][0]
    reviewed = tools["record_maintenance_finding"].fn(
        namespace="research", session_id=started["session_id"],
        command_key="review-health", expected_revision=started["revision"],
        finding_id=finding["id"], action="reviewed", observation="No failures found",
    )
    assessed = tools["assess_maintenance_health"].fn(
        namespace="research", session_id=started["session_id"],
        command_key="assess-health", expected_revision=reviewed["revision"],
        acceptable=True, criteria="No blocking failures",
        observation="No blocking failures found",
    )
    assert assessed["unmet_completion_checks"] == []
    assert _required_scopes("knowledge_engine_mcp", "read", "scan_intake_maintenance") == [
        "knowledge:intake:read",
    ]
    assert _required_scopes("knowledge_engine_mcp", "write", "record_maintenance_finding") == [
        "knowledge:intake:write",
    ]
    scopes.remove("namespace:research:read")
    denied = tools["scan_intake_maintenance"].fn(namespace="research")
    assert denied["error"]["code"] == "unauthorized"
