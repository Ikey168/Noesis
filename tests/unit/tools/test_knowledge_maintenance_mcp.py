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
        "derived_object_revision",
        "derived_object_history",
        "derived_object_generation_delta",
        "replay_derived_object_generations",
        "derived_object_lineage",
        "explain_derived_object_invalidation",
        "derived_projection",
        "derived_object_health",
        "begin_research_snapshot",
        "inspect_research_snapshot",
        "renew_research_snapshot",
        "close_research_snapshot",
        "research_snapshot_pins",
        "research_snapshot_health",
        "classify_epistemic_statement",
        "register_epistemic_taxonomy",
        "list_epistemic_taxonomies",
        "assess_epistemic_statement",
        "review_epistemic_status",
        "get_epistemic_assessment",
        "search_epistemic_assessments",
        "explain_epistemic_assessment",
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
        "derived_object_revision",
        "derived_object_history",
        "derived_object_generation_delta",
        "replay_derived_object_generations",
        "derived_object_lineage",
        "explain_derived_object_invalidation",
        "derived_projection",
        "derived_object_health",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:read"
        ]
    for name in (
        "begin_research_snapshot",
        "renew_research_snapshot",
        "close_research_snapshot",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:snapshot:write"
        ]
    for name in (
        "inspect_research_snapshot",
        "research_snapshot_pins",
        "research_snapshot_health",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:snapshot:read"
        ]
    assert _mutability("assess_epistemic_statement") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "assess_epistemic_statement"
    ) == ["knowledge:epistemic:write"]
    assert _mutability("register_epistemic_taxonomy") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "register_epistemic_taxonomy"
    ) == ["knowledge:epistemic:write"]
    assert _mutability("review_epistemic_status") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "review_epistemic_status"
    ) == ["knowledge:epistemic:review"]
    for name in (
        "classify_epistemic_statement",
        "get_epistemic_assessment",
        "search_epistemic_assessments",
        "explain_epistemic_assessment",
        "list_epistemic_taxonomies",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:epistemic:read"
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
    assert "immutable-derived-object-revisions" in capabilities["features"]
    assert "support-aware-truth-maintenance" in capabilities["features"]
    assert "snapshot-pinned-research-sessions" in capabilities["features"]
    assert "noesis-epistemic-taxonomy-v1" in capabilities["contracts"]
    assert "noesis-epistemic-assessment-v1" in capabilities["contracts"]
    assert "noesis-epistemic-explanation-v1" in capabilities["contracts"]
    assert "versioned-epistemic-status" in capabilities["features"]
    assert "evidence-calibrated-assessments" in capabilities["features"]
    assert "reviewed-epistemic-overrides" in capabilities["features"]
