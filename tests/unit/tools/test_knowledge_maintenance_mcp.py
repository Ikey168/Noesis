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
        "capture_claim_timeline_state",
        "link_claim_evolution",
        "get_claim_timeline_state",
        "detect_claim_successors",
        "diff_claim_timeline_states",
        "get_claim_evolution_timeline",
        "compare_claim_sources",
        "replay_claim_evolution",
        "register_evidence_freshness_policy",
        "get_evidence_freshness_policy",
        "annotate_evidence_freshness",
        "relate_evidence_applicability",
        "review_evidence_freshness_override",
        "assess_evidence_freshness",
        "get_evidence_freshness_assessment",
        "list_expiring_evidence",
        "simulate_evidence_freshness_policy",
        "compare_evidence_freshness_policies",
        "register_evidence_freshness_dependency",
        "propagate_evidence_freshness",
        "replay_evidence_freshness_assessment",
        "register_research_gap_policy",
        "record_research_coverage",
        "discover_research_gaps",
        "get_research_gap",
        "explain_research_gap",
        "list_research_gaps",
        "update_research_gap_status",
        "prioritize_research_gaps",
        "list_research_gap_tasks",
        "compare_research_gap_coverage",
        "replay_research_gap",
        "register_source_capability",
        "get_source_capability",
        "create_source_research_objective",
        "preview_source_acquisition_plan",
        "create_source_acquisition_plan",
        "get_source_acquisition_plan",
        "explain_source_acquisition_plan",
        "execute_source_acquisition_plan",
        "cancel_source_acquisition_plan",
        "inspect_source_acquisition_run",
        "replay_source_acquisition_run",
        "register_dataset_catalog",
        "get_dataset_catalog",
        "search_datasets",
        "register_dataset_release",
        "get_dataset_release",
        "ingest_tabular_dataset",
        "replay_tabular_ingestion",
        "slice_dataset_table",
        "compare_dataset_releases",
        "suggest_dataset_joins",
        "preview_dataset_join",
        "accept_dataset_join",
        "get_dataset_lineage",
        "register_methodology_study",
        "get_methodology_study",
        "search_methodology_studies",
        "extract_methodology_statements",
        "replay_methodology_extraction",
        "assess_methodology_limitation",
        "list_methodology_limitations",
        "link_study_artifact",
        "get_study_replication_graph",
        "compare_study_methodologies",
        "explain_study_evidence_strength",
        "register_multimodal_asset",
        "get_multimodal_asset",
        "search_multimodal_assets",
        "get_multimodal_segment",
        "extract_multimodal_observations",
        "replay_multimodal_extraction",
        "link_cross_modal_evidence",
        "record_media_transformation",
        "assess_media_authenticity",
        "inspect_media_provenance",
        "register_citation_archive_policy",
        "get_citation_archive_policy",
        "capture_citation_snapshot",
        "get_citation_snapshot",
        "replay_citation_snapshot",
        "verify_preserved_citation",
        "record_citation_health",
        "get_citation_status",
        "preview_citation_repair",
        "accept_citation_repair",
        "export_preserved_citations",
        "register_change_brief_policy",
        "preview_semantic_change",
        "generate_change_brief",
        "get_change_brief",
        "list_change_briefs",
        "compare_change_briefs",
        "replay_change_brief",
        "create_change_brief_subscription",
        "deliver_change_briefs",
        "acknowledge_change_brief_delivery",
        "review_change_brief",
        "export_change_briefs",
        "validate_research_recipe",
        "register_research_recipe",
        "list_research_recipes",
        "preview_research_recipe",
        "run_research_recipe",
        "get_research_recipe_run",
        "cancel_research_recipe_run",
        "replay_research_recipe_run",
        "export_research_recipe_run",
        "register_quality_policy",
        "get_quality_policy",
        "assess_knowledge_quality",
        "get_quality_assessment",
        "replay_quality_assessment",
        "aggregate_quality_assessments",
        "rank_by_quality",
        "simulate_quality_policy",
        "compare_quality_policies",
        "review_quality_override",
        "inspect_quality_health",
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
    for name in ("capture_claim_timeline_state", "link_claim_evolution"):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:claim-timeline:write"
        ]
    for name in (
        "get_claim_timeline_state",
        "detect_claim_successors",
        "diff_claim_timeline_states",
        "get_claim_evolution_timeline",
        "compare_claim_sources",
        "replay_claim_evolution",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:claim-timeline:read"
        ]
    for name in (
        "register_evidence_freshness_policy",
        "annotate_evidence_freshness",
        "relate_evidence_applicability",
        "assess_evidence_freshness",
        "register_evidence_freshness_dependency",
        "propagate_evidence_freshness",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:freshness:write"
        ]
    assert _mutability("review_evidence_freshness_override") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "review_evidence_freshness_override"
    ) == ["knowledge:freshness:review"]
    for name in (
        "get_evidence_freshness_policy",
        "get_evidence_freshness_assessment",
        "list_expiring_evidence",
        "simulate_evidence_freshness_policy",
        "compare_evidence_freshness_policies",
        "replay_evidence_freshness_assessment",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:freshness:read"
        ]
    for name in (
        "register_research_gap_policy",
        "record_research_coverage",
        "discover_research_gaps",
        "prioritize_research_gaps",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:gaps:write"
        ]
    assert _mutability("update_research_gap_status") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "update_research_gap_status"
    ) == ["knowledge:gaps:review"]
    for name in (
        "get_research_gap",
        "explain_research_gap",
        "list_research_gaps",
        "list_research_gap_tasks",
        "compare_research_gap_coverage",
        "replay_research_gap",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:gaps:read"
        ]
    for name in (
        "register_source_capability",
        "create_source_research_objective",
        "create_source_acquisition_plan",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:source-planner:write"
        ]
    for name in (
        "execute_source_acquisition_plan",
        "cancel_source_acquisition_plan",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:source-planner:execute"
        ]
    for name in (
        "get_source_capability",
        "preview_source_acquisition_plan",
        "get_source_acquisition_plan",
        "explain_source_acquisition_plan",
        "inspect_source_acquisition_run",
        "replay_source_acquisition_run",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:source-planner:read"
        ]
    for name in (
        "register_dataset_catalog",
        "register_dataset_release",
        "accept_dataset_join",
    ):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:dataset:write"
        ]
    assert _mutability("ingest_tabular_dataset") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "ingest_tabular_dataset"
    ) == ["knowledge:dataset:ingest"]
    assert _mutability("preview_dataset_join") == "read"
    assert _required_scopes("knowledge_engine_mcp", "read", "preview_dataset_join") == [
        "knowledge:dataset:calculate"
    ]
    for name in (
        "get_dataset_catalog",
        "search_datasets",
        "get_dataset_release",
        "replay_tabular_ingestion",
        "slice_dataset_table",
        "compare_dataset_releases",
        "suggest_dataset_joins",
        "get_dataset_lineage",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:dataset:read"
        ]
    for name in ("register_methodology_study", "link_study_artifact"):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:methodology:write"
        ]
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "extract_methodology_statements"
    ) == ["knowledge:methodology:extract"]
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "assess_methodology_limitation"
    ) == ["knowledge:methodology:review"]
    for name in (
        "get_methodology_study",
        "search_methodology_studies",
        "replay_methodology_extraction",
        "list_methodology_limitations",
        "get_study_replication_graph",
        "compare_study_methodologies",
        "explain_study_evidence_strength",
    ):
        assert _mutability(name) == "read"
        assert _required_scopes("knowledge_engine_mcp", "read", name) == [
            "knowledge:methodology:read"
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
    assert "noesis-claim-state-v1" in capabilities["contracts"]
    assert "noesis-claim-lineage-v1" in capabilities["contracts"]
    assert "noesis-claim-successor-match-v1" in capabilities["contracts"]
    assert "noesis-claim-timeline-v1" in capabilities["contracts"]
    assert "noesis-claim-semantic-diff-v1" in capabilities["contracts"]
    assert "claim-evolution-lineage" in capabilities["features"]
    assert "explainable-claim-successor-matching" in capabilities["features"]
    assert "semantic-claim-state-diffs" in capabilities["features"]
    assert "snapshot-consistent-claim-timelines" in capabilities["features"]
    assert "noesis-evidence-freshness-policy-v1" in capabilities["contracts"]
    assert "noesis-evidence-freshness-assessment-v1" in capabilities["contracts"]
    assert "noesis-evidence-applicability-relation-v1" in capabilities["contracts"]
    assert "noesis-evidence-freshness-impact-v1" in capabilities["contracts"]
    assert "versioned-evidence-freshness-policies" in capabilities["features"]
    assert "side-effect-free-freshness-simulation" in capabilities["features"]
    assert "noesis-research-gap-policy-v1" in capabilities["contracts"]
    assert "noesis-research-coverage-v1" in capabilities["contracts"]
    assert "noesis-research-gap-v1" in capabilities["contracts"]
    assert "noesis-research-gap-task-v1" in capabilities["contracts"]
    assert "noesis-research-gap-report-v1" in capabilities["contracts"]
    assert "multidimensional-research-gap-records" in capabilities["features"]
    assert "deterministic-budgeted-research-planning" in capabilities["features"]
    assert "noesis-source-capability-v1" in capabilities["contracts"]
    assert "noesis-source-research-objective-v1" in capabilities["contracts"]
    assert "noesis-source-acquisition-plan-v1" in capabilities["contracts"]
    assert "noesis-source-plan-receipt-v1" in capabilities["contracts"]
    assert "credential-safe-source-capability-registry" in capabilities["features"]
    assert "checkpointed-source-plan-execution" in capabilities["features"]
    assert "noesis-dataset-catalog-v1" in capabilities["contracts"]
    assert "noesis-dataset-release-v1" in capabilities["contracts"]
    assert "noesis-tabular-ingestion-receipt-v1" in capabilities["contracts"]
    assert "noesis-dataset-slice-v1" in capabilities["contracts"]
    assert "noesis-dataset-join-v1" in capabilities["contracts"]
    assert "versioned-dataset-table-column-identities" in capabilities["features"]
    assert "bounded-multiformat-tabular-ingestion" in capabilities["features"]
    assert "noesis-methodology-study-v1" in capabilities["contracts"]
    assert "noesis-methodology-extraction-v1" in capabilities["contracts"]
    assert "noesis-methodology-assessment-v1" in capabilities["contracts"]
    assert "noesis-study-artifact-link-v1" in capabilities["contracts"]
    assert "noesis-methodology-comparison-v1" in capabilities["contracts"]
    assert "exact-locator-method-extraction" in capabilities["features"]
    assert "study-artifact-and-replication-graphs" in capabilities["features"]
    assert "noesis-multimodal-asset-v1" in capabilities["contracts"]
    assert "noesis-multimodal-extraction-v1" in capabilities["contracts"]
    assert "noesis-cross-modal-evidence-v1" in capabilities["contracts"]
    assert "noesis-media-authenticity-v1" in capabilities["contracts"]
    assert "noesis-multimodal-search-v1" in capabilities["contracts"]
    assert "noesis-citation-archive-policy-v1" in capabilities["contracts"]
    assert "noesis-citation-snapshot-v1" in capabilities["contracts"]
    assert "noesis-citation-verification-v1" in capabilities["contracts"]
    assert "noesis-citation-health-v1" in capabilities["contracts"]
    assert "noesis-citation-export-v1" in capabilities["contracts"]
    assert "noesis-semantic-change-event-v1" in capabilities["contracts"]
    assert "noesis-change-brief-policy-v1" in capabilities["contracts"]
    assert "noesis-change-brief-v1" in capabilities["contracts"]
    assert "noesis-change-brief-delivery-v1" in capabilities["contracts"]
    assert "noesis-change-brief-export-v1" in capabilities["contracts"]
    assert "noesis-research-recipe-v1" in capabilities["contracts"]
    assert "noesis-research-recipe-preview-v1" in capabilities["contracts"]
    assert "noesis-research-recipe-run-v1" in capabilities["contracts"]
    assert "noesis-research-recipe-receipt-v1" in capabilities["contracts"]
    assert "noesis-research-recipe-export-v1" in capabilities["contracts"]
    assert "noesis-quality-policy-v1" in capabilities["contracts"]
    assert "noesis-quality-assessment-v1" in capabilities["contracts"]
    assert "noesis-quality-collection-v1" in capabilities["contracts"]
    assert "noesis-quality-ranking-v1" in capabilities["contracts"]
    assert "noesis-quality-health-v1" in capabilities["contracts"]
