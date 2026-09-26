"""Optional machine priority suggestions beside independent review votes."""

import os


def register(mcp, safe, context):
    def advisor(conn, *, initialize=True):
        from src.integrations.typesafe import TypeSafeClient
        from src.kb.decision_runtime import DecisionRuntime
        from src.kb.jev_review_priority import ReviewPriorityAdvisor
        runtime = DecisionRuntime(
            conn, client=TypeSafeClient(), initialize=initialize,
            credential_resolver=lambda reference: (
                os.environ.get("TYPESAFE_API_KEY") if reference == "typesafe" else None))
        return ReviewPriorityAdvisor(conn, runtime, initialize=initialize)

    @mcp.tool()
    def suggest_jev_review_priority(namespace: str, task_id: str, run_id: str,
                                    policy: dict, max_cost_usd_micros: int,
                                    network_approved: bool = False,
                                    max_attempts: int = 1, deadline_s: float = 30) -> dict:
        """Assess impact after current coordinator, target, and source checks."""
        principal_id, scopes = context()
        return safe(lambda conn: advisor(conn).suggest(
            namespace, task_id, run_id, principal_id=principal_id, scopes=scopes,
            allow_remote=network_approved, policy=policy,
            max_attempts=max_attempts, max_cost_usd_micros=max_cost_usd_micros,
            deadline_s=deadline_s),
            write=True, required_scope="knowledge:decision:execute")

    @mcp.tool()
    def inspect_jev_review_priority(namespace: str, task_id: str, run_id: str) -> dict:
        """Recheck current task/vote/source state before showing a retained suggestion."""
        principal_id, scopes = context()
        return safe(lambda conn: advisor(conn, initialize=False).inspect(
            namespace, task_id, run_id, principal_id=principal_id, scopes=scopes),
            required_scope="knowledge:inbox:read")
