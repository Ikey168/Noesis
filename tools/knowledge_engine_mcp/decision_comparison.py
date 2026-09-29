"""Read-only, revision-pinned comparison for a recorded decision."""

from src.kb.decisions import DecisionStore


def register(mcp, safe, context):
    @mcp.tool()
    def inspect_decision_comparative_matrix(
        namespace: str, decision_id: str, receipt_id: str,
    ) -> dict:
        """Show options, missing inputs, declared tradeoffs, and sensitivity scenarios."""
        return safe(
            lambda conn: DecisionStore(conn, initialize=False).comparative_matrix(
                namespace, decision_id, receipt_id,
                principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:decisions:read",
        )
