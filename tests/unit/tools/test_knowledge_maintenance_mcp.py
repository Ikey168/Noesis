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
        "create_hypothesis_workspace",
        "get_hypothesis_workspace",
        "revise_hypothesis_workspace",
        "branch_hypothesis_workspace",
        "retire_hypothesis_workspace",
        "link_hypothesis_evidence",
        "retract_hypothesis_evidence",
        "compare_hypotheses",
        "create_hypothesis_research_plan",
        "get_hypothesis_research_plan",
        "execute_hypothesis_research_plan",
        "export_hypothesis_workspace",
        "replay_hypothesis_workspace",
        "register_source_identity",
        "lookup_source_identity",
        "source_identity_history",
        "revise_source_identity",
        "delete_source_identity",
        "decide_source_alias",
        "split_source_alias",
        "resolve_source_alias",
        "add_source_relationship",
        "retract_source_relationship",
        "source_identity_dossier",
        "source_relationship_path",
        "explain_source_independence",
        "create_event_record",
        "revise_event_record",
        "get_event_record",
        "get_event_record_as_of",
        "ingest_event_mentions",
        "attach_event_account",
        "retract_event_account",
        "list_event_accounts",
        "relate_events",
        "search_event_records",
        "event_timeline",
        "event_neighborhood",
        "diff_event_revisions",
        "replay_event_record",
        "register_quantitative_unit",
        "register_quantitative_metric",
        "revise_quantitative_metric",
        "record_quantitative_observation",
        "record_quantitative_series_break",
        "discover_quantitative_metrics",
        "get_quantitative_metric",
        "read_quantitative_series",
        "assess_quantitative_comparability",
        "convert_quantitative_value",
        "evaluate_quantitative_formula",
        "transform_quantitative_frequency",
        "adjust_quantitative_inflation",
        "replay_quantitative_calculation",
        "register_geospatial_place",
        "revise_geospatial_place",
        "get_geospatial_place",
        "store_geospatial_geometry",
        "list_geospatial_geometries",
        "simplify_geospatial_geometry",
        "resolve_geospatial_candidates",
        "record_geospatial_resolution",
        "review_geospatial_resolution",
        "calculate_spatial_relation",
        "replay_spatial_relation",
        "search_geospatial_knowledge",
        "query_geospatial_event_map",
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
        "create_hypothesis_workspace",
        "revise_hypothesis_workspace",
        "branch_hypothesis_workspace",
        "retire_hypothesis_workspace",
        "link_hypothesis_evidence",
        "retract_hypothesis_evidence",
        "create_hypothesis_research_plan",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:hypothesis:write"
        ]
    assert _mutability("execute_hypothesis_research_plan") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "execute_hypothesis_research_plan"
    ) == ["knowledge:hypothesis:execute"]
    for name in (
        "get_hypothesis_workspace",
        "compare_hypotheses",
        "get_hypothesis_research_plan",
        "export_hypothesis_workspace",
        "replay_hypothesis_workspace",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:hypothesis:read"
        ]
    for name in ("decide_source_alias", "split_source_alias"):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:source-identity:review"
        ]
    for name in (
        "register_source_identity",
        "revise_source_identity",
        "delete_source_identity",
        "add_source_relationship",
        "retract_source_relationship",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:source-identity:write"
        ]
    for name in (
        "lookup_source_identity",
        "source_identity_history",
        "resolve_source_alias",
        "source_identity_dossier",
        "source_relationship_path",
        "explain_source_independence",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:source-identity:read"
        ]
    for name in (
        "create_event_record",
        "revise_event_record",
        "ingest_event_mentions",
        "attach_event_account",
        "relate_events",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:event:write"
        ]
    assert _mutability("retract_event_account") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "retract_event_account"
    ) == ["knowledge:event:review"]
    for name in (
        "get_event_record",
        "get_event_record_as_of",
        "list_event_accounts",
        "search_event_records",
        "event_timeline",
        "event_neighborhood",
        "diff_event_revisions",
        "replay_event_record",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:event:read"
        ]
    for name in (
        "register_quantitative_unit",
        "register_quantitative_metric",
        "revise_quantitative_metric",
        "record_quantitative_observation",
        "record_quantitative_series_break",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:quantitative:write"
        ]
    for name in (
        "convert_quantitative_value",
        "evaluate_quantitative_formula",
        "transform_quantitative_frequency",
        "adjust_quantitative_inflation",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:quantitative:calculate"
        ]
    for name in (
        "discover_quantitative_metrics",
        "get_quantitative_metric",
        "read_quantitative_series",
        "assess_quantitative_comparability",
        "replay_quantitative_calculation",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:quantitative:read"
        ]
    for name in (
        "register_geospatial_place",
        "revise_geospatial_place",
        "store_geospatial_geometry",
        "simplify_geospatial_geometry",
        "record_geospatial_resolution",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:geospatial:write"
        ]
    assert _mutability("review_geospatial_resolution") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "review_geospatial_resolution"
    ) == ["knowledge:geospatial:review"]
    assert _mutability("calculate_spatial_relation") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "calculate_spatial_relation"
    ) == ["knowledge:geospatial:calculate"]
    for name in (
        "get_geospatial_place",
        "list_geospatial_geometries",
        "resolve_geospatial_candidates",
        "replay_spatial_relation",
        "search_geospatial_knowledge",
        "query_geospatial_event_map",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:geospatial:read"
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
    assert "noesis-hypothesis-workspace-v1" in capabilities["contracts"]
    assert "noesis-hypothesis-comparison-v1" in capabilities["contracts"]
    assert "noesis-hypothesis-research-plan-v1" in capabilities["contracts"]
    assert "noesis-hypothesis-export-v1" in capabilities["contracts"]
    assert "versioned-hypothesis-workspaces" in capabilities["features"]
    assert "independence-aware-hypothesis-comparison" in capabilities["features"]
    assert "resumable-hypothesis-research-plans" in capabilities["features"]
    assert "noesis-source-identity-v1" in capabilities["contracts"]
    assert "noesis-source-alias-decision-v1" in capabilities["contracts"]
    assert "noesis-source-relationship-v1" in capabilities["contracts"]
    assert "noesis-source-dossier-v1" in capabilities["contracts"]
    assert "noesis-source-independence-v1" in capabilities["contracts"]
    assert "canonical-source-identities" in capabilities["features"]
    assert "reversible-source-alias-resolution" in capabilities["features"]
    assert "time-bounded-source-ownership-graph" in capabilities["features"]
    assert "source-aware-evidence-independence" in capabilities["features"]
    assert "noesis-event-record-v2" in capabilities["contracts"]
    assert "noesis-event-mention-v1" in capabilities["contracts"]
    assert "noesis-event-account-v1" in capabilities["contracts"]
    assert "noesis-event-relation-v1" in capabilities["contracts"]
    assert "noesis-event-search-v1" in capabilities["contracts"]
    assert "event-centric-knowledge-model" in capabilities["features"]
    assert "multilingual-event-mention-clustering" in capabilities["features"]
    assert "competing-event-accounts" in capabilities["features"]
    assert "snapshot-bound-event-search" in capabilities["features"]
    assert "noesis-quantitative-metric-v1" in capabilities["contracts"]
    assert "noesis-quantitative-observation-v1" in capabilities["contracts"]
    assert "noesis-quantitative-calculation-v1" in capabilities["contracts"]
    assert "noesis-quantitative-comparability-v1" in capabilities["contracts"]
    assert "versioned-quantitative-semantics" in capabilities["features"]
    assert "vintage-aware-observations" in capabilities["features"]
    assert "reproducible-quantitative-transformations" in capabilities["features"]
    assert "series-break-comparability" in capabilities["features"]
    assert "noesis-geospatial-place-v1" in capabilities["contracts"]
    assert "noesis-geospatial-geometry-v1" in capabilities["contracts"]
    assert "noesis-geocode-resolution-v1" in capabilities["contracts"]
    assert "noesis-spatial-result-v1" in capabilities["contracts"]
    assert "versioned-place-gazetteer" in capabilities["features"]
    assert "time-bounded-wgs84-geometry" in capabilities["features"]
    assert "ambiguity-preserving-geocoding" in capabilities["features"]
    assert "reproducible-spatial-relations" in capabilities["features"]
    assert "bounded-event-map-queries" in capabilities["features"]
