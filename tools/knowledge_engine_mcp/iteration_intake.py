"""Typed measured Iteration cycle for Noesis-owned playbooks."""

from src.kb.intake_iteration import IntakeIterationStore


def register(mcp, safe, context):
    @mcp.tool()
    def start_intake_iteration(
        namespace: str, request_key: str, playbook_id: str,
        expected_revision: int, expected: str, stability_criteria: str,
        intent: str, origin_session_id: str | None = None,
    ) -> dict:
        """Start a version-pinned, measured cycle on a Noesis playbook."""
        return safe(
            lambda conn: IntakeIterationStore(conn).start(
                namespace, request_key, playbook_id=playbook_id,
                expected_revision=expected_revision, expected=expected,
                stability_criteria=stability_criteria, intent=intent,
                origin_session_id=origin_session_id,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def start_intake_decision_iteration(
        namespace: str, request_key: str, decision_id: str,
        expected_revision: int, expected: str, stability_criteria: str,
        intent: str, origin_session_id: str | None = None,
    ) -> dict:
        """Start a version-pinned, measured cycle on a Noesis Decision Record."""
        return safe(
            lambda conn: IntakeIterationStore(conn).start_decision(
                namespace, request_key, decision_id=decision_id,
                expected_revision=expected_revision, expected=expected,
                stability_criteria=stability_criteria, intent=intent,
                origin_session_id=origin_session_id,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def start_intake_report_iteration(
        namespace: str, request_key: str, report_id: str,
        expected_revision: int, expected: str, stability_criteria: str,
        intent: str, origin_session_id: str | None = None,
    ) -> dict:
        """Start a version-pinned, measured cycle on a Noesis authored report."""
        return safe(
            lambda conn: IntakeIterationStore(conn).start_report(
                namespace, request_key, report_id=report_id,
                expected_revision=expected_revision, expected=expected,
                stability_criteria=stability_criteria, intent=intent,
                origin_session_id=origin_session_id,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def start_intake_concept_iteration(
        namespace: str, request_key: str, bundle_id: str, concept_id: str,
        expected_revision: int, expected: str, stability_criteria: str,
        intent: str, origin_session_id: str | None = None,
    ) -> dict:
        """Start a version-pinned, measured cycle on a Research Bundle concept."""
        return safe(
            lambda conn: IntakeIterationStore(conn).start_concept(
                namespace, request_key, bundle_id=bundle_id, concept_id=concept_id,
                expected_revision=expected_revision, expected=expected,
                stability_criteria=stability_criteria, intent=intent,
                origin_session_id=origin_session_id,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def start_intake_modulo_note_iteration(
        namespace: str, request_key: str, plugin_link: dict, source_snapshot: dict,
        expected: str, stability_criteria: str, intent: str,
        origin_session_id: str | None = None,
    ) -> dict:
        """Start a local candidate cycle pinned to a Modulo note identity and version."""
        return safe(
            lambda conn: IntakeIterationStore(conn).start_modulo_note(
                namespace, request_key, plugin_link=plugin_link,
                source_snapshot=source_snapshot, expected=expected,
                stability_criteria=stability_criteria, intent=intent,
                origin_session_id=origin_session_id,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def record_intake_iteration_outcome(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int, observed: str, learning: str,
        measurements: list[dict], uncertainty: str, external_causes: str,
        evidence: list[dict],
    ) -> dict:
        """Record caller-observed measurements, uncertainty, and evidence links."""
        return safe(
            lambda conn: IntakeIterationStore(conn).record_outcome(
                namespace, session_id, command_key,
                expected_revision=expected_revision, observed=observed,
                learning=learning, measurements=measurements,
                uncertainty=uncertainty, external_causes=external_causes,
                evidence=evidence, principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def propose_intake_playbook_revision(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int, title: str, prerequisites: list[str],
        environment: str, steps: list[dict], verification: str,
        source_rationale: str, before_after_rationale: str,
    ) -> dict:
        """Save a reviewable proposed playbook change after measuring an outcome."""
        return safe(
            lambda conn: IntakeIterationStore(conn).propose(
                namespace, session_id, command_key,
                expected_revision=expected_revision, title=title,
                prerequisites=prerequisites, environment=environment,
                steps=steps, verification=verification,
                source_rationale=source_rationale,
                before_after_rationale=before_after_rationale,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def propose_intake_decision_revision(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int, content: dict, before_after_rationale: str,
    ) -> dict:
        """Save a complete, validated Decision Record proposal for review."""
        return safe(
            lambda conn: IntakeIterationStore(conn).propose_decision(
                namespace, session_id, command_key,
                expected_revision=expected_revision, content=content,
                before_after_rationale=before_after_rationale,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def propose_intake_report_revision(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int, content: dict, before_after_rationale: str,
    ) -> dict:
        """Save a complete, validated Authored Report proposal for review."""
        return safe(
            lambda conn: IntakeIterationStore(conn).propose_report(
                namespace, session_id, command_key,
                expected_revision=expected_revision, content=content,
                before_after_rationale=before_after_rationale,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def propose_intake_concept_revision(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int, concept: dict, before_after_rationale: str,
    ) -> dict:
        """Save a complete cited Research Bundle concept proposal for review."""
        return safe(
            lambda conn: IntakeIterationStore(conn).propose_concept(
                namespace, session_id, command_key,
                expected_revision=expected_revision, concept=concept,
                before_after_rationale=before_after_rationale,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def propose_intake_modulo_note_revision(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int, content: dict, before_after_rationale: str,
    ) -> dict:
        """Save a Noesis-local note candidate; this tool never writes to Modulo."""
        return safe(
            lambda conn: IntakeIterationStore(conn).propose_modulo_note(
                namespace, session_id, command_key,
                expected_revision=expected_revision, content=content,
                before_after_rationale=before_after_rationale,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def accept_intake_playbook_revision(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int,
    ) -> dict:
        """Accept the proposal into playbook history with an iteration receipt."""
        return safe(
            lambda conn: IntakeIterationStore(conn).accept(
                namespace, session_id, command_key,
                expected_revision=expected_revision,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def accept_intake_decision_revision(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int,
    ) -> dict:
        """Accept the reviewed Decision Record proposal with its Iteration receipt."""
        return safe(
            lambda conn: IntakeIterationStore(conn).accept_decision(
                namespace, session_id, command_key,
                expected_revision=expected_revision,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def accept_intake_report_revision(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int,
    ) -> dict:
        """Accept a reviewed report change into its history with an Iteration receipt."""
        return safe(
            lambda conn: IntakeIterationStore(conn).accept_report(
                namespace, session_id, command_key,
                expected_revision=expected_revision,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def accept_intake_concept_revision(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int,
    ) -> dict:
        """Accept a concept proposal into the Research Bundle's revision history."""
        return safe(
            lambda conn: IntakeIterationStore(conn).accept_concept(
                namespace, session_id, command_key,
                expected_revision=expected_revision,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def accept_intake_modulo_note_revision(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int,
    ) -> dict:
        """Accept a local candidate snapshot without claiming Modulo writeback."""
        return safe(
            lambda conn: IntakeIterationStore(conn).accept_modulo_note(
                namespace, session_id, command_key,
                expected_revision=expected_revision,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def review_intake_iteration_stability(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int, stable: bool, observation: str,
    ) -> dict:
        """Record caller review of the cycle's declared stability criteria."""
        return safe(
            lambda conn: IntakeIterationStore(conn).review_stability(
                namespace, session_id, command_key,
                expected_revision=expected_revision, stable=stable,
                observation=observation,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def route_intake_iteration_gap(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int, target_mode: str, reason: str, intent: str,
        inputs: dict | None = None, references: list[dict] | None = None,
    ) -> dict:
        """Atomically route an observed Iteration gap with parent and child lineage."""
        return safe(
            lambda conn: IntakeIterationStore(conn).route_gap(
                namespace, session_id, command_key,
                expected_revision=expected_revision, target_mode=target_mode,
                reason=reason, intent=intent, inputs=inputs,
                references=references, principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )
