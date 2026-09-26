"""Versioned skills with separated practice, procedure and reviewer evidence."""

from src.kb.intake_skills import IntakeSkillStore


def register(mcp, safe, context):
    @mcp.tool()
    def create_intake_skill(
        namespace: str, request_key: str, name: str, description: str,
        mastery_criteria: list[str], practice_links: list[dict] | None = None,
        procedure_links: list[dict] | None = None, plugin_links: list[dict] | None = None,
    ) -> dict:
        """Create a stable skill linking practice cards and procedures to explicit mastery criteria."""
        return safe(
            lambda conn: IntakeSkillStore(conn).create(
                namespace, request_key, name=name, description=description,
                mastery_criteria=mastery_criteria, practice_links=practice_links or [],
                procedure_links=procedure_links or [], plugin_links=plugin_links,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def revise_intake_skill(
        namespace: str, skill_id: str, command_key: str, expected_revision: int,
        mastery_criteria: list[str], practice_links: list[dict] | None = None,
        procedure_links: list[dict] | None = None,
    ) -> dict:
        """Revise criteria or links; earlier independent assessments stop counting."""
        return safe(
            lambda conn: IntakeSkillStore(conn).revise(
                namespace, skill_id, command_key, expected_revision=expected_revision,
                mastery_criteria=mastery_criteria, practice_links=practice_links or [],
                procedure_links=procedure_links or [],
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def assess_intake_skill(
        namespace: str, skill_id: str, command_key: str, expected_revision: int,
        criterion: str, passed: bool, evidence_note: str,
    ) -> dict:
        """Record an independent reviewer's pass/fail judgement for one mastery criterion."""
        return safe(
            lambda conn: IntakeSkillStore(conn).record_assessment(
                namespace, skill_id, command_key, expected_revision=expected_revision,
                criterion=criterion, passed=passed, evidence_note=evidence_note,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:review",
        )

    @mcp.tool()
    def inspect_intake_skill_evidence(namespace: str, skill_id: str) -> dict:
        """List dated evidence by basis; mastery needs independent assessment of every criterion."""
        return safe(
            lambda conn: IntakeSkillStore(conn, initialize=False).evidence(
                namespace, skill_id, principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )
