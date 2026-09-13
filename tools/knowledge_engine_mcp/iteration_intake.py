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
