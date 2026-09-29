"""Optional semantic relevance over eligible source planner capabilities."""

import os


def register(mcp, safe, context):
    @mcp.tool()
    def suggest_jev_source_relevance(namespace: str, objective_id: str, run_id: str,
                                     at_ms: int, policy: dict,
                                     max_cost_usd_micros: int,
                                     network_approved: bool = False,
                                     max_attempts: int = 1,
                                     deadline_s: float = 30) -> dict:
        """Compare eligible source relevance without persisting or executing a plan."""
        from src.integrations.typesafe import TypeSafeClient
        from src.kb.decision_runtime import DecisionRuntime
        from src.kb.jev_source_planning import PlanningInputResolver, suggest_source_relevance
        from src.kb.source_planner import SourcePlannerStore

        principal_id, scopes = context()

        def action(conn):
            planner = SourcePlannerStore(conn, initialize=False)
            runtime = DecisionRuntime(
                conn, client=TypeSafeClient(), input_resolver=PlanningInputResolver(conn),
                credential_resolver=lambda reference: (
                    os.environ.get("TYPESAFE_API_KEY") if reference == "typesafe" else None))
            return suggest_source_relevance(
                runtime, planner, namespace, objective_id, run_id,
                at_ms=at_ms, principal_id=principal_id, scopes=scopes,
                credential_available=lambda ref: bool(os.environ.get(ref)),
                allow_remote=network_approved, policy=policy,
                max_cost_usd_micros=max_cost_usd_micros,
                max_attempts=max_attempts, deadline_s=deadline_s)

        return safe(action, write=True, required_scope="knowledge:decision:execute")
