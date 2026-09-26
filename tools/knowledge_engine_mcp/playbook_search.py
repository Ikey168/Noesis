"""Current-access search for guided Noesis procedures."""

from src.kb.intake_playbook_search import IntakePlaybookSearch


def register(mcp, safe, context):
    @mcp.tool()
    def search_intake_playbooks(
        namespace: str, task: str, environment: str | None = None,
        limit: int = 20,
    ) -> dict:
        """Find owner-visible playbooks by task and environment with exact revisions."""
        return safe(
            lambda conn: IntakePlaybookSearch(conn).search(
                namespace, task, environment=environment, limit=limit,
                principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )
