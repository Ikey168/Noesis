# Information intake milestone verification

This is the recorded check for [#2465](https://github.com/Ikey168/Noesis/issues/2465):
do the ten mode issues ([#1570](https://github.com/Ikey168/Noesis/issues/1570)–[#1579](https://github.com/Ikey168/Noesis/issues/1579))
and the shared foundations ([#1580](https://github.com/Ikey168/Noesis/issues/1580)–[#1583](https://github.com/Ikey168/Noesis/issues/1583))
meet their acceptance criteria through supported MCP operations? Verified on
2026-09-29 against the code on this branch.

Each row maps an acceptance criterion to the MCP operations that provide it
(on the `noesis-knowledge-engine` server unless stated) and to the offline test that proves it. Test IDs
are relative to `tests/unit/` unless they start with `integration/`.

## What the evidence is, and what it is not

All evidence here is **deterministic CI evidence**: fixtures, an in-memory or
file-backed DuckDB warehouse, and a real MCP client (`fastmcp.Client`, plus the
stdio and Streamable HTTP integration tests). It shows that the operation
exists, is reachable over MCP, enforces its scopes and revisions, replays, and
survives a store restart.

It is not, and is not recorded as:

- signed-in Modulo evidence (a Modulo plugin record changed and returned to
  the same record, a second device, offline Modulo);
- live-source or model-quality evidence (real feeds, real pages, suggestion
  quality);
- human-assessed outcomes (a documented decision that was right, a fix that
  unblocked the user, unaided mastery);
- cadence measurements (15 minutes of Awareness a day, the Exploration time
  box, 30–60 minutes of Maintenance a month).

Those remain open under [#1781](https://github.com/Ikey168/Noesis/issues/1781).
Every "Modulo handoff" checklist item on the mode issues depends on the Modulo
repository and is recorded below as **#1781**, not as met.

## Status values

| Status | Meaning |
| --- | --- |
| **met** | The Noesis-side criterion is implemented, exposed over MCP, and proven by the cited offline test. |
| **met (limit)** | Met on the Noesis side; the stated limitation is part of the result, not hidden. |
| **#1781** | Depends on the Modulo repository, a signed-in journey, live sources, or human assessment. Not met here. |
| **not met** | Could be done in Noesis and is not. |

No criterion is recorded as **not met**. Gaps found during this check (no
MCP-client test for signal-rule previews, procedure search, step automation,
Iteration gap routing, Creation builds, and no reconciliation report for
browser-local records) were closed by
`tools/test_intake_milestone_gaps_mcp.py` and the new
`reconcile_modulo_intake_migration` operation.

## Awareness — #1570

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Subscribed RSS/Atom and configured newsletter inputs land in a persistent unread inbox with original locators and publication times | `subscribe_intake_feed`, `subscribe_intake_newsletter_input`, `ingest_intake_newsletter_message`, `refresh_intake_feed_inbox`, `list_intake_feed_inbox` | `kb/test_intake_inbox.py::test_refresh_parses_configured_feed_and_retains_triage`, `::test_explicit_newsletter_input_preserves_message_locator_and_triage`, `tools/test_intake_modes_mcp.py::test_awareness_newsletter_input_over_mcp` | met (limit): newsletter sender is `caller_supplied_unverified`; no mailbox adapter |
| Scan, read/unread, discard/archive, flag, escalate; batch triage and deduplication keep decisions across refresh and restart | `mark_intake_feed_read`, `decide_intake_feed_item`, `triage_awareness_item`, `triage_awareness_batch` | `kb/test_intake_inbox.py::test_feed_inbox_preserves_decisions_across_refresh_and_restart`, `::test_duplicate_feed_item_and_batch_triage`, `tools/test_intake_modes_mcp.py::test_awareness_mcp_uses_persistent_feed_inbox` | met |
| Changes and explainable signal filters without forcing a research project | `preview_intake_feed_signals`, `save_intake_feed_signal_rule`, `list_intake_feed_signal_rules`, `preview_intake_feed_signal_rule` | `kb/test_intake_inbox.py::test_saved_signals_are_versioned_scoped_and_non_mutating`, `tools/test_intake_milestone_gaps_mcp.py::test_saved_signal_rule_preview_explains_matches_without_mutating_the_inbox` | met (limit): keyword matching only |
| Escalate into Exploration, Research or Problem-Solving with source and annotations intact | `annotate_intake_feed_item`, `promote_awareness_item` | `kb/test_intake_inbox.py::test_escalation_preserves_source_and_annotation_identity`, `tools/test_intake_native_journeys_mcp.py::test_awareness_discard_and_awareness_exploration_research` | met |
| Remaining items and session time shown; completion reflects actual triage | `start_awareness_from_inbox`, `list_intake_feed_inbox` (`remaining_unprocessed`), `inspect_intake_mode`, `export_modulo_intake_handoff` v2 | `kb/test_intake_inbox.py::test_awareness_triage_updates_queue_and_inbox_atomically`, `kb/test_intake_modes.py::test_mode_requires_recorded_outcome_and_survives_reopen` | met |
| Whole mode over MCP with durable state, actionable errors, restart/replay | as above | `tools/test_intake_native_journeys_mcp.py::test_awareness_discard_and_awareness_exploration_research`, `integration/test_intake_mcp_stdio.py::test_awareness_to_exploration_survives_server_restart` | met |
| Modulo handoff: same-record triage, retry, correction, revocation; 15-minute budget in Modulo | — | — | #1781 |

## Exploration — #1571

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Lightweight session without objective, taxonomy or project | `start_intake_mode` (Exploration), `command_intake_mode` | `tools/test_intake_native_journeys_mcp.py::test_awareness_discard_and_awareness_exploration_research` (no-output session) | met |
| Capture URLs, readable content where permitted, annotations and the trail; keep source identity and notes | `capture_exploration_page`, `annotate_exploration_source`, `inspect_exploration_source`, `visit_exploration_feed_item` | `kb/test_intake_exploration.py::test_lightweight_capture_versions_and_replay`, `::test_annotations_follow_source_versions_and_owner`, `::test_fetch_replay_avoids_network_and_rejects_unsafe_urls`, `tools/test_intake_modes_mcp.py::test_exploration_capture_over_mcp` | met (limit): public HTTPS pages only |
| Related sources and unexpected connections with provenance; dismiss or follow | `suggest_exploration_sources`, `decide_exploration_suggestion` | `kb/test_intake_exploration.py::test_related_source_provenance_follow_dismiss_and_replay`, `::test_feed_visit_anchors_related_sources_without_recapture` | met (limit): `lexical_overlap_v1`; suggestion quality unassessed |
| Pause/resume, time budget, valid outcome with no artifact | `command_intake_mode` (pause/resume/complete) | `kb/test_intake_modes.py::test_exploration_timebox_uses_observed_elapsed_time`, `::test_pause_conflicts_and_reference_access` | met |
| Promote discoveries without copying away identity | `start_intake_research_topic`, `start_intake_mode` with `origin` | `kb/test_intake_exploration.py::test_escalated_feed_item_keeps_identity_in_exploration`, `kb/test_intake_research_topic.py::test_research_topic_pins_owned_source_and_reports_correction` | met |
| Whole mode over MCP with durable state and replay | as above | `tools/test_intake_native_journeys_mcp.py::test_awareness_discard_and_awareness_exploration_research` | met |
| Modulo handoff: launch from a Modulo item, device pause/resume, promotion of a saved Modulo record | — | — | #1781 |

## Deep Research — #1572

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Linked topics, sources, Evidence Cards, concepts, claims, reports, maps, questions with exact citation spans | `start_intake_research_topic`, `save_intake_research_bundle`, `inspect_intake_research_bundle` | `kb/test_intake_research_bundle.py::test_bundle_requires_exact_pinned_spans_and_gates_research_completion`, `tools/test_intake_research_bundle_mcp.py::test_research_bundle_mcp_discovery_and_scopes` | met (limit): spans are checked against pinned feed and Exploration revisions only |
| Scope, questions, budget, reviewable Definition of Done first; active-topic limit enforced | `start_intake_research_topic` | `kb/test_intake_research_topic.py::test_research_topic_links_project_replays_and_rolls_back_at_capacity`, `kb/test_intake_modes.py::test_active_research_limit_is_configurable`, `::test_paused_research_topic_frees_active_slot` | met |
| Acquisition, extraction, assessment and synthesis composed into a resumable workflow with blockers and stage receipts | `inspect_intake_research_progress`, `assess_intake_research_progress`, `inspect_intake_research_assessment` | `kb/test_intake_research_bundle.py::test_research_progress_assessment_is_durable_and_replays_its_snapshot`, `::test_progress_assessment_joins_failed_recipe_receipts_and_coverage` | met (limit): a read-side composition of existing loop and recipe receipts, not an automated acquisition-to-bundle pipeline |
| Bundle with Evidence Cards, Concepts, Claim Ledger, L1 Brief, Mental Model, Maps; interpretation marked | `save_intake_research_bundle` | `kb/test_intake_research_bundle.py::test_bundle_requires_exact_pinned_spans_and_gates_research_completion` | met |
| Supporting/contradicting evidence, independence, unresolved questions, coverage; count is not confidence | `review_research_claim_independence`, `inspect_intake_research_progress` | `kb/test_intake_research_bundle.py::test_independence_review_is_bound_to_exact_bundle_claim_revision` | met (limit): independence is a recorded human review, not machine-established lineage |
| Completion against the Definition of Done; verifiable portable export; insufficiency stays explicit | `command_intake_mode` (complete), `export_intake_research_bundle`, `verify_intake_research_bundle_export` | `kb/test_intake_research_bundle.py::test_bundle_completion_and_owner_access`, `tools/test_intake_native_journeys_mcp.py::test_fresh_deployment_import_interruption_duplicates_revocation_correction_export` | met |
| Whole mode over MCP with durable state and replay | as above | `tools/test_intake_research_topic_mcp.py::test_research_topic_mcp_start_replay_and_scope`, `tools/test_intake_native_journeys_mcp.py::test_awareness_discard_and_awareness_exploration_research` | met |
| Modulo handoff: `ResearchProject` mapping, return artifacts, representative live topic | — | — | #1781 |

## Decision Support — #1573

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Decision with alternatives, constraints, deadline, stakes, required confidence; simple yes/no | `create_research_decision`, `inspect_research_decision` | `kb/test_decisions.py::test_standalone_choice_requires_explicit_bounded_context`, `::test_standalone_choice_is_versioned_and_requires_current_decision_access` | met |
| Only decision-relevant evidence, with an explicit stop condition | `record_decision_evidence` | `kb/test_decisions.py::test_decision_evidence_is_budgeted_assessed_and_durably_replayed`, `tools/test_decisions_mcp.py::test_bounded_decision_evidence_mcp_uses_decision_scope_and_returns_receipt` | met (limit): relevance is author-assessed |
| Comparative Matrix with missing inputs, assumptions, tradeoffs, sensitivity | `calculate_decision_sensitivity`, `inspect_decision_comparative_matrix` | `kb/test_decisions.py::test_sensitivity_changes_order_preserves_ties_and_records_provenance`, `::test_comparative_matrix_is_pinned_to_decision_revision_and_scope`, `tools/test_decision_comparison_mcp.py::test_decision_comparison_mcp` | met (limit): utilities are declared, not recommendations |
| Chosen option, rationale, evidence snapshot, uncertainty, review trigger recorded | `create_research_decision`, `command_intake_mode` | `kb/test_intake_decision_handoff.py::test_standalone_choice_completes_linked_mode_once_and_preserves_origin` | met |
| Close independently of open research; reopen or revise with history | `revise_research_decision`, `create_decision_condition_watch`, `poll_decision_condition_watch` | `kb/test_decisions.py::test_revision_preserves_what_was_known_and_rechecks_project_access`, `tools/test_decision_alerts_mcp.py::test_public_decision_alert_delivery_and_acknowledgement` | met |
| Whole mode over MCP with durable state and replay | as above | `tools/test_intake_native_journeys_mcp.py::test_research_decision_creation_externalization_iteration` | met |
| Modulo handoff: task/project return links, changed-source reopen of the Modulo record | — | — | #1781 |

## Problem-Solving — #1574

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Symptom, environment, urgency and success check without a research bundle | `start_problem_session` | `kb/test_intake_problem.py::test_problem_trail_replay_restart_and_verified_completion` | met |
| Hypotheses, references, attempts, observations, next action | `record_problem_step` | `kb/test_intake_problem.py::test_problem_trail_replay_restart_and_verified_completion` | met |
| Bounded authorized execution through configured integrations; proposals for actions beyond authority | `propose_problem_action`, `preview_problem_action`, `consent_problem_action`, `execute_problem_action` | `kb/test_intake_problem_actions.py::test_problem_action_is_proposed_consented_and_executed_once`, `::test_problem_action_unavailable_stale_consent_and_indeterminate_retry`, `tools/test_intake_problem_actions_mcp.py::test_problem_action_mcp_scopes_and_truthful_unavailable_result` | met (limit): the public adapter registry is empty, so a deployment must configure an integration; the MCP path reports `unavailable` |
| Resumable trail; suggested fix distinct from verified fix | `record_problem_step`, `inspect_intake_mode` | `tools/test_intake_native_journeys_mcp.py::test_problem_to_playbook_and_research_to_internalization` | met |
| Resolve only with a recorded verification; promote to playbook or practice | `command_intake_mode` (complete), `promote_problem_playbook` | `kb/test_intake_playbooks.py::test_problem_playbook_guided_failure_recovery_and_replay` | met (limit): the verification is caller-reported |
| Whole mode over MCP with durable state and replay | as above | `tools/test_intake_native_journeys_mcp.py::test_problem_to_playbook_and_research_to_internalization` | met |
| Modulo handoff: launch from a Modulo task, Blueprint correlation, live verified fix | — | — | #1781 |

## Creation — #1575

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Project with audience, artifact type, purpose, criteria, linked inputs | `start_intake_creation` | `kb/test_intake_creation.py::test_creation_requires_current_report_and_all_author_review_checks` | met |
| Draft, revision, review, finished with history; references survive editing | `command_intake_creation`, `inspect_intake_creation`, `revise_authored_report` | `kb/test_intake_creation.py::test_creation_requires_current_report_and_all_author_review_checks`, `tools/test_intake_creation_mcp.py::test_creation_mcp_round_trip_and_denied_read` | met |
| Export posts, documentation, teaching material; other types through explicit adapters | `export_intake_creation`, `build_intake_creation_artifact` | `kb/test_intake_creation_builds.py::test_build_adapter_runs_once_and_export_carries_receipt`, `tools/test_intake_milestone_gaps_mcp.py::test_creation_build_types_are_explicit_without_a_configured_adapter` | met (limit): no build adapter is configured on the public server |
| Unsupported types and unavailable builds reported honestly | `start_intake_creation`, `build_intake_creation_artifact` | `kb/test_intake_creation_builds.py::test_missing_adapter_unsupported_type_and_unfinished_project_are_explicit`, `tools/test_intake_milestone_gaps_mcp.py::test_creation_build_types_are_explicit_without_a_configured_adapter` | met |
| Criteria checked before completion; handoff to Externalization/Internalization | `command_intake_creation`, `handoff_intake_creation` | `tools/test_intake_native_journeys_mcp.py::test_research_decision_creation_externalization_iteration` | met (limit): the review is author-reported |
| Export separate from publishing | `export_intake_creation` (`publication_authorized: false`) | `tools/test_intake_native_journeys_mcp.py::test_research_decision_creation_externalization_iteration` | met |
| Whole mode over MCP | as above | `tools/test_intake_creation_mcp.py::test_creation_mcp_round_trip_and_denied_read` | met |
| Modulo handoff: draft authority between stores, Modulo publish consent | — | — | #1781 |

## Externalization — #1576

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Versioned playbooks, checklists, templates, defaults, automation rules from completed work | `promote_problem_playbook`, `promote_intake_work_procedure`, `revise_intake_playbook` | `kb/test_intake_playbook_automation.py::test_completed_work_becomes_each_procedure_kind_as_a_draft`, `::test_invalid_kind_fields_are_rejected` | met |
| Prerequisites, steps, constraints, expected results, verification, recovery, rationale | `inspect_intake_playbook` | `kb/test_intake_playbooks.py::test_problem_playbook_guided_failure_recovery_and_replay` | met |
| Retrieve by task/context; guided execution with outcomes and revision provenance | `search_intake_playbooks`, `start_guided_playbook_run`, `command_guided_playbook_run`, `inspect_guided_playbook_run` | `kb/test_intake_playbook_search.py::test_task_environment_search_filters_by_owner_and_source_access`, `tools/test_intake_milestone_gaps_mcp.py::test_procedure_search_and_unconfigured_automation_stay_draft` | met (limit): lexical search |
| Supported automation with previews, scoped authorization, idempotency, receipts | `preview_playbook_step_automation`, `execute_playbook_step_automation` | `kb/test_intake_playbook_automation.py::test_automated_step_executes_once_and_raises_trust_to_executed_verified`, `::test_unconfigured_or_uncertain_adapter_never_counts_as_execution`, `tools/test_intake_milestone_gaps_mcp.py::test_procedure_search_and_unconfigured_automation_stay_draft` | met (limit): no adapter configured publicly |
| Trusted status only from an actual rehearsal or execution | `inspect_intake_playbook` (`trust_state`) | `tools/test_intake_native_journeys_mcp.py::test_research_decision_creation_externalization_iteration` | met (limit): `rehearsed_reported` is caller-reported |
| Concepts linked to Internalization; execution distinct from mastery | `promote_problem_playbook` (`concept_references`), `create_intake_skill` | `kb/test_intake_skills.py::test_procedure_rehearsal_is_procedural_evidence_not_mastery` | met |
| Whole mode over MCP | as above | `tools/test_intake_native_journeys_mcp.py::test_research_decision_creation_externalization_iteration` | met |
| Modulo handoff: SOP/runbook links, Blueprint execution through the same connector | — | — | #1781 |

## Internalization — #1577

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Reviewable retrieval packs from concepts/evidence with references and reversible editing | `draft_intake_practice_pack`, `create_reviewed_intake_practice_pack`, `create_practice_pack`, `revise_practice_pack` | `kb/test_intake_practice_drafts.py::test_research_bundle_concepts_and_cards_can_be_drafted_and_pause_after_correction`, `tools/test_intake_practice_mcp.py::test_practice_draft_mcp_accepts_selected_bundle_concepts_and_evidence` | met (limit): answers are `author_supplied_unverified` |
| Scheduled spaced repetition, durable history, configurable schedule, overdue state | `list_due_practice`, `start_practice_review`, `command_practice_review` | `kb/test_intake_practice.py::test_schedule_configuration_and_write_only_mutation_scope`, `::test_revision_resets_due_schedule_without_rewriting_historical_review` | met |
| Unaided attempt before reveal; assisted performance distinguished | `command_practice_review` | `kb/test_intake_practice.py::test_practice_attempt_before_reveal_and_restart_replay` | met (limit): self-reported |
| Procedural and explanation practice with criteria and dated evidence | `create_intake_skill`, `inspect_intake_skill_evidence`, `assess_intake_skill` | `kb/test_intake_skills.py::test_skill_identity_evidence_bases_and_independent_mastery`, `::test_assisted_attempts_and_unknown_links_are_not_mastery` | met |
| Modulo practice-plugin migration and portable export; scheduling stays in Noesis | `preview_modulo_intake_migration`, `import_modulo_flashcards`, `export_practice_pack`, `verify_practice_export` | `kb/test_intake_modulo_migration.py::test_flashcard_import_preserves_source_history_and_requires_schedule_review`, `tools/test_intake_practice_mcp.py::test_practice_public_mcp_round_trip_and_revocation` | met (limit): fixture data; a migration of real plugin state is #1781 |
| Source corrections propagate for review without erasing history | `list_due_practice`, `scan_intake_maintenance` | `kb/test_intake_practice.py::test_corrected_intake_source_pauses_practice_until_card_revision`, `::test_corrected_or_withdrawn_document_pauses_practice_without_erasing_history` | met (limit): opaque reference kinds report `not_checked` |
| Whole mode over MCP | as above | `tools/test_intake_native_journeys_mcp.py::test_problem_to_playbook_and_research_to_internalization` | met |
| Modulo handoff and human-assessed mastery | — | — | #1781 |

## Iteration — #1578

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Expected outcomes, observations, measurements attached to the original decision, playbook, concept or artifact | `start_intake_iteration`, `start_intake_decision_iteration`, `start_intake_report_iteration`, `start_intake_concept_iteration`, `record_intake_iteration_outcome` | `kb/test_intake_iteration.py::test_iteration_accepts_measured_playbook_revision_and_reopens_new_cycle`, `::test_decision_iteration_writes_reviewed_measurement_to_decision_history_and_replays`, `::test_concept_iteration_updates_pinned_research_bundle_with_receipt` | met (limit): observations are caller-reported |
| Expected versus actual with uncertainty and external causes; no causal claim | `record_intake_iteration_outcome` | `kb/test_intake_iteration.py::test_iteration_accepts_measured_playbook_revision_and_reopens_new_cycle` | met |
| Revisions proposed and reviewed with before/after rationale | `propose_intake_*_revision` | `tools/test_intake_iteration_mcp.py::test_iteration_mcp_discovery_and_scope`, `::test_decision_iteration_mcp_discovery_and_authoritative_revision`, `::test_report_iteration_mcp_discovery_and_authoritative_revision` | met |
| Accepted revisions written into the authoritative history | `accept_intake_*_revision` | `kb/test_intake_iteration.py::test_report_iteration_commits_revision_and_receipt_atomically_and_requires_current_revision` | met (limit): a Modulo-owned note only gets a Noesis-local candidate (`writeback_state=not_written`) |
| Gaps routed to any upstream mode with lineage | `route_intake_iteration_gap` | `kb/test_intake_iteration.py::test_iteration_gap_handoff_is_atomic_replayable_and_access_checked`, `tools/test_intake_milestone_gaps_mcp.py::test_iteration_gap_routes_upstream_with_lineage_and_replays` | met |
| Cycle completion, stability criteria, continuation and reopening | `review_intake_iteration_stability`, `command_intake_mode` | `kb/test_intake_iteration.py::test_iteration_accepts_measured_playbook_revision_and_reopens_new_cycle` | met |
| Whole mode over MCP | as above | `tools/test_intake_native_journeys_mcp.py::test_research_decision_creation_externalization_iteration` | met |
| Modulo handoff: revision returned to the same plugin record | — | — | #1781 |

## Maintenance — #1579

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Cross-mode review queue: broken sources, stale procedures, old topics, outdated citations, duplicates, overdue reviews, failed automation | `scan_intake_maintenance` | `kb/test_intake_maintenance.py::test_maintenance_reviews_corrected_research_sources_without_leaking_other_projects`, `::test_maintenance_finds_outdated_report_citations_without_copying_source_content`, `::test_maintenance_reports_duplicate_and_unlinked_reports_with_owner_and_link_scoping`, `::test_maintenance_flags_only_stale_open_research_topics_with_project_access` | met (limit): Modulo-side records are not scanned |
| Explainable reasons and links, not an opaque score | `scan_intake_maintenance` | `kb/test_intake_maintenance.py::test_maintenance_flags_a_finished_creation_after_its_report_changes` | met |
| Resumable time-boxed checklist with defer, refresh, repair, archive, authorized deletion | `start_intake_maintenance`, `record_maintenance_finding`, `execute_intake_maintenance_action` | `kb/test_intake_maintenance_actions.py::test_failed_job_refresh_requeues_once_and_replays_receipt`, `::test_superseded_research_source_repair_repins_current_revision`, `::test_stale_research_topic_archive_keeps_prior_revision_readable`, `::test_delete_requires_confirmation_retention_scopes_and_honours_holds` | met |
| Dependency impact preview; retention holds, provenance and history preserved | `preview_intake_maintenance_impact` | `kb/test_intake_maintenance.py::test_maintenance_impact_preview_is_scoped_read_only_and_names_dependents`, `::test_failed_worker_impact_resolves_source_pack_and_retention_dependents` | met (limit): Noesis dependents only |
| Existing scheduler/worker reused; failures, retry, last successful runs over MCP | `inspect_maintenance_job`, `retry_maintenance_job`, `maintenance_health` | `kb/test_intake_maintenance.py::test_maintenance_worker_failures_are_scoped_and_survive_restart`, `tools/test_knowledge_maintenance_mcp.py::test_maintenance_surface_and_catalog_scope_separation` | met |
| Session completes against checklist and health while deferrals stay visible | `assess_maintenance_health`, `command_intake_mode` | `kb/test_intake_maintenance.py::test_maintenance_snapshot_replay_and_typed_completion` | met |
| Whole mode over MCP | as above | `tools/test_intake_maintenance_mcp.py::test_maintenance_mcp_review_and_denied_read`, `tools/test_intake_native_journeys_mcp.py::test_maintenance_over_stale_sources_procedures_learning_and_automation` | met |
| Modulo handoff: Modulo dependents in impact previews, live monthly review | — | — | #1781 |

## Workflow foundations — #1580

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Sessions with mode-specific intent, budget, outputs, status, completion criteria | `discover_intake_modes`, `start_intake_mode`, `inspect_intake_mode` | `kb/test_intake_modes.py::test_mode_catalog_is_complete`, `::test_mode_requires_recorded_outcome_and_survives_reopen` | met |
| Lightweight selection from the ten questions with user override | `route_intake_mode` | `kb/test_jev_intake_routing.py::test_explicit_answers_and_override_never_use_hosted_inference`, `tools/test_intake_modes_mcp.py::test_jev_free_text_intake_route_mcp_explicit_precedence` | met |
| Stable identities and links for every object kind | `start_intake_mode` (`references`, `plugin_links`) | `kb/test_intake_modes.py::test_plugin_aware_v3_handoff_preserves_versioned_identity_and_references`, `::test_initial_document_revision_is_a_valid_intake_reference` | met; see the [identity map](../architecture/intake-cross-repo-identity-map.md) |
| Common paths: discard, bidirectional Externalization/Internalization, Iteration to any upstream mode | `start_intake_mode` with `origin`, `route_intake_iteration_gap`, `handoff_intake_creation` | `kb/test_intake_cross_mode_mcp_journey.py::test_ten_mode_handoffs_discard_pause_replay_export_and_revocation` | met |
| Provenance, access, context, references carried without duplicating artifacts; why/when recorded | `start_intake_mode`, `export_modulo_intake_handoff` | `kb/test_intake_modes.py::test_handoff_replay_active_limit_and_access_revocation` | met |
| Interrupt/resume, cadence, three active topics by default, Maintenance in parallel | `command_intake_mode`, `start_intake_mode` (`cadence`) | `kb/test_intake_modes.py::test_active_research_limit_is_configurable`, `::test_pause_conflicts_and_reference_access` | met (limit): `cadence` is recorded, not scheduled |
| Cross-mode transitions, interrupted handoffs, idempotent retries, revocation, no-structure sessions tested | all intake tools | `kb/test_intake_cross_mode_mcp_journey.py::test_ten_mode_handoffs_discard_pause_replay_export_and_revocation` | met |
| Cross-repo mapping, authority per field, round trips through the same mapping | `export_modulo_intake_handoff` v3, `recheck_modulo_plugin_link` | `kb/test_modulo_plugin_link_access.py::test_modulo_link_recheck_distinguishes_access_and_version_states` | Noesis side published in the [identity map](../architecture/intake-cross-repo-identity-map.md); Modulo round trips are #1781 |

## Migration — #1581

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Inventory installed plugins, scopes, record counts, schemas, persistence | `preview_modulo_intake_migration` (server-side callback reader `ModuloStateInventoryClient`) | `kb/test_intake_modulo_migration.py::test_callback_pages_feed_durable_preview_with_receipt_and_unknown_metadata` | #1781: the reader exists; no inventory of the user's installed plugins has been taken |
| Preflight IDs, versions, mappings, locators, relations, attachments, duplicates, unsupported fields | `preview_modulo_intake_migration`, `inspect_modulo_intake_migration` | `kb/test_intake_modulo_migration.py::test_preview_preserves_plugin_identity_and_flags_legacy_review`, `::test_duplicate_records_and_unknown_versions_fail_before_write` | met |
| Link authenticated records in place; resumable imports with conflict previews and reconciliation counts | `preview_modulo_intake_migration`, `reconcile_modulo_intake_migration`, `inspect_modulo_intake_reconciliation` | `kb/test_intake_modulo_migration.py::test_reconciliation_reports_migration_and_conflict_outcomes_without_claiming_replacement`, `tools/test_intake_milestone_gaps_mcp.py::test_browser_local_reconciliation_over_mcp_reports_outcomes_and_writes_nothing` | met (limit) on the Noesis side: the report and its counts exist; plugin imports and conflict resolution are Modulo's (#1781) |
| Preserve user edits and plugin history; snapshots and editable copies distinct | `start_intake_mode` (`plugin_links.representation`) | `kb/test_intake_modes.py::test_plugin_aware_v3_handoff_preserves_versioned_identity_and_references` | met (limit): Noesis records the distinction; preserving plugin history is #1781 |
| Map supported records to Noesis objects with access-checked references; report unsupported fields | `preview_modulo_intake_migration` (`mappings`) | `kb/test_intake_modulo_migration.py::test_document_mappings_require_current_document_access_on_create_and_inspect` | met |
| Flashcard IDs, review logs, schedule parameters preserved; scheduler conversion reviewed | `import_modulo_flashcards` | `kb/test_intake_modulo_migration.py::test_flashcard_import_preserves_source_history_and_requires_schedule_review` | met |
| Native triage, annotation, research, decision, procedure and practice from plugin surfaces returning to the same records | — | — | #1781 |
| Second device, offline replay, restart, duplicates, conflicts, correction, revocation, export and restore with live plugin data | — | — | #1781 |

## MCP foundations — #1582

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| One supported entry point with unambiguous tools | `noesis-knowledge-engine` server | `tools/test_intake_modes_mcp.py::test_intake_public_tools_and_access` | met |
| Caller-scoped discovery with inputs, outputs, readiness, next actions | `discover_intake_workflows`, `discover_intake_modes` | `kb/test_intake_readiness.py::test_discovery_lists_authorized_steps_for_durable_sessions` | met |
| Preflight with actionable blockers; catalog presence is not readiness | `preflight_intake_mode` | `kb/test_intake_readiness.py::test_readiness_distinguishes_scopes_subscriptions_and_unknown_live_state`, `tools/test_intake_readiness_mcp.py::test_scoped_readiness_mcp_and_denied_namespace` | met (limit): live state is reported as `unknown_live_or_fixture` |
| Durable progress, budgets, partial artifacts, pause/cancel/resume, provenance | `inspect_intake_mode`, `command_intake_mode`, `export_intake_mode`, `verify_intake_mode_export` | `kb/test_intake_modes.py::test_mode_requires_recorded_outcome_and_survives_reopen` | met |
| Resources, templates and prompts with access control and versioned references | `noesis://intake/...` resource templates, `start-information-intake` prompt | `kb/test_intake_mcp_resources.py::test_discovered_session_resources_and_prompt_recheck_current_access` | met |
| Same schemas, authorization and errors over stdio and Streamable HTTP | both transports | `integration/test_intake_mcp_http.py::test_http_tokens_isolate_intake_sessions`, `integration/test_intake_mcp_stdio.py::test_awareness_to_exploration_survives_server_restart`, `tools/test_intake_http_context.py::test_http_intake_context_uses_authenticated_caller` | met |
| Continuous monitoring with durable, opt-in delivery and acknowledgement | `poll_decision_condition_watch`; `claim_subscription_deliveries`, `acknowledge_subscription_delivery` on the `noesis-subscriptions` server | `tools/test_subscription_delivery_mcp.py::test_subscription_delivery_tools_auth_and_lifecycle`, `tools/test_decision_alerts_mcp.py::test_public_decision_alert_delivery_and_acknowledgement` | met (limit): destinations must be configured by the deployment |
| Real transport discovery, calls, artifacts, unavailable services, unauthorized requests, restart | as above | `integration/test_intake_mcp_http.py::test_http_tokens_isolate_intake_sessions`, `tools/test_intake_problem_actions_mcp.py::test_problem_action_mcp_scopes_and_truthful_unavailable_result` | met |
| Modulo connector contract published; links rendered by Modulo | `export_modulo_intake_handoff` v1–v3 | contracts under `contracts/schemas/jsonschema/noesis-modulo-intake-handoff-v*.json` | Noesis contract published; rendering in Modulo is #1781 |

## Workflow acceptance — #1583

| Criterion | MCP operations | Evidence | Status |
| --- | --- | --- | --- |
| Mode matrix published | — | [intake acceptance matrix](../subsystems/intake-acceptance-matrix.md), this document | met |
| Awareness → Discard and Awareness → Exploration → Research through a real MCP client, incl. no-output Exploration | see #1570, #1571, #1572 | `tools/test_intake_native_journeys_mcp.py::test_awareness_discard_and_awareness_exploration_research` | met |
| Research → Decision → Creation → Externalization → Iteration | see above | `tools/test_intake_native_journeys_mcp.py::test_research_decision_creation_externalization_iteration` | met |
| Problem → verified fix → Playbook; Research/Creation → Internalization | see above | `tools/test_intake_native_journeys_mcp.py::test_problem_to_playbook_and_research_to_internalization` | met |
| Maintenance over stale sources, broken automation, outdated procedures and learning material | see #1579 | `tools/test_intake_native_journeys_mcp.py::test_maintenance_over_stale_sources_procedures_learning_and_automation` | met |
| Fresh deployment, import, interruption, duplicates, insufficient evidence, revocation, correction, export | see above | `tools/test_intake_native_journeys_mcp.py::test_fresh_deployment_import_interruption_duplicates_revocation_correction_export` | met |
| Deterministic, real-source and user-assessed evidence separated; outcomes recorded | — | — | #1781 |
| Cadence measured against the time boxes | — | — | #1781 |
| Signed-in Modulo journeys, browser-local migration on real data, second device | — | — | #1781 |

## Running the evidence

```bash
python -m pytest -q tests/unit/kb/test_intake_*.py tests/unit/kb/test_modulo_plugin_link_access.py \
  tests/unit/kb/test_jev_intake_routing.py tests/unit/kb/test_decisions.py tests/unit/tools/test_intake_*.py \
  tests/unit/tools/test_decision_comparison_mcp.py tests/unit/tools/test_decisions_mcp.py \
  tests/unit/tools/test_decision_alerts_mcp.py tests/unit/tools/test_knowledge_maintenance_mcp.py \
  tests/unit/tools/test_subscription_delivery_mcp.py
python -m pytest -q tests/integration/test_intake_mcp_http.py tests/integration/test_intake_mcp_stdio.py
```
