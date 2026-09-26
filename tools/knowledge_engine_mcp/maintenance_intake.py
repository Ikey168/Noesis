"""Typed monthly cross-mode intake review over the existing session ledger."""

from src.kb.intake_maintenance import IntakeMaintenanceStore
from src.kb.intake_maintenance_impact import IntakeMaintenanceImpact


def register(mcp, safe, context):
    @mcp.tool()
    def scan_intake_maintenance(namespace: str) -> dict:
        """Explain overdue practice, stale research topics, drafts, and maintenance failures."""
        return safe(
            lambda conn: IntakeMaintenanceStore(conn).scan(
                namespace, principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def start_intake_maintenance(
        namespace: str, request_key: str, intent: str,
        duration_minutes: int = 45,
    ) -> dict:
        """Snapshot a bounded, explainable 30–60 minute Maintenance review."""
        return safe(
            lambda conn: IntakeMaintenanceStore(conn).start(
                namespace, request_key, intent=intent,
                duration_minutes=duration_minutes,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def record_maintenance_finding(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int, finding_id: str, action: str,
        observation: str,
    ) -> dict:
        """Record a reported review disposition; this does not execute a repair."""
        return safe(
            lambda conn: IntakeMaintenanceStore(conn).record_finding(
                namespace, session_id, command_key,
                expected_revision=expected_revision, finding_id=finding_id,
                action=action, observation=observation,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def assess_maintenance_health(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int, acceptable: bool,
        criteria: str, observation: str,
    ) -> dict:
        """Record a caller-reviewed health criterion before session completion."""
        return safe(
            lambda conn: IntakeMaintenanceStore(conn).assess_health(
                namespace, session_id, command_key,
                expected_revision=expected_revision, acceptable=acceptable,
                criteria=criteria, observation=observation,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def execute_intake_maintenance_action(
        namespace: str, session_id: str, command_key: str,
        expected_revision: int, finding_id: str, action: str,
        preview_hash: str, confirm_delete: bool = False,
    ) -> dict:
        """Execute a previewed refresh, archive, or retention-checked delete for a finding."""
        from src.kb.intake_maintenance_actions import IntakeMaintenanceActions

        return safe(
            lambda conn: IntakeMaintenanceActions(conn).execute(
                namespace, session_id, command_key,
                expected_revision=expected_revision, finding_id=finding_id,
                action=action, reviewed_preview_hash=preview_hash,
                confirm_delete=confirm_delete,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def preview_intake_maintenance_impact(
        namespace: str, finding_id: str, action: str,
    ) -> dict:
        """Inspect local dependents and retention state before refresh, archive, or delete."""
        return safe(
            lambda conn: IntakeMaintenanceImpact(conn).preview(
                namespace, finding_id, action,
                principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )
