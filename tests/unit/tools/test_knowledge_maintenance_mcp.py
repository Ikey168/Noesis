from __future__ import annotations

import asyncio
import inspect

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_maintenance_surface_and_catalog_scope_separation(monkeypatch):
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "run_maintenance_once",
        "run_maintenance_drain",
        "list_maintenance_jobs",
        "list_maintenance_due_work",
        "inspect_maintenance_job",
        "set_maintenance_schedule_paused",
        "pause_maintenance_schedule",
        "resume_maintenance_schedule",
        "cancel_maintenance_job",
        "retry_maintenance_job",
        "recover_stale_maintenance_jobs",
        "inspect_maintenance_generation",
        "replay_maintenance_generation",
        "maintenance_generation_lineage",
        "maintenance_health",
        "document_revision",
        "document_revision_history",
        "document_generation_delta",
        "replay_document_generation_delta",
        "document_revision_health",
    }
    assert expected <= set(tools)
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "run_maintenance_once"
    ) == ["operator"]
    for name in (
        "document_revision",
        "document_revision_history",
        "document_generation_delta",
        "replay_document_generation_delta",
        "document_revision_health",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:read"
        ]
    for name in (
        "pause_maintenance_schedule",
        "resume_maintenance_schedule",
        "cancel_maintenance_job",
        "retry_maintenance_job",
        "recover_stale_maintenance_jobs",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:maintenance:admin"
        ]

    monkeypatch.setattr(server, "_context", lambda: ("reader", {"knowledge:read"}))
    denied = call(tools["pause_maintenance_schedule"], pack_id="missing")
    assert denied == {
        "ok": False,
        "error": {
            "code": "unauthorized",
            "message": "knowledge:maintenance:admin scope is required",
        },
    }


def test_capabilities_advertise_generation_contracts():
    tools = asyncio.run(server.mcp.get_tools())
    capabilities = call(tools["knowledge_engine_capabilities"])
    assert "noesis-maintenance-job-receipt-v1" in capabilities["contracts"]
    assert "noesis-knowledge-generation-v1" in capabilities["contracts"]
    assert "noesis-document-generation-delta-v1" in capabilities["contracts"]
    assert "knowledge-maintenance" in capabilities["features"]
    assert "immutable-document-revisions" in capabilities["features"]
