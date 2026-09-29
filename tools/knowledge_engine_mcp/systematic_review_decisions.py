"""Optional source-bound machine screening tools for systematic reviews."""

import os


def register(mcp, safe, context):
    def store(conn, *, initialize=True):
        from src.integrations.typesafe import TypeSafeClient
        from src.kb.decision_runtime import DecisionRuntime
        from src.kb.systematic_review_decisions import ScreeningDecisionStore

        runtime = DecisionRuntime(
            conn, client=TypeSafeClient(), initialize=initialize,
            credential_resolver=lambda reference: (
                os.environ.get("TYPESAFE_API_KEY") if reference == "typesafe" else None),
        )
        return ScreeningDecisionStore(conn, runtime, initialize=initialize)

    @mcp.tool()
    def suggest_systematic_review_screening(namespace: str, candidate_id: str, stage: str,
                                            run_id: str, policy: dict, max_cost_usd_micros: int,
                                            network_approved: bool = False, max_attempts: int = 1,
                                            deadline_s: float = 30) -> dict:
        """Suggest eligibility criterion statuses without recording a human vote."""
        principal_id, scopes = context()
        return safe(
            lambda conn: store(conn).suggest(
                namespace, candidate_id, stage, run_id, principal_id=principal_id,
                scopes=scopes, allow_remote=network_approved, policy=policy,
                max_attempts=max_attempts, max_cost_usd_micros=max_cost_usd_micros,
                deadline_s=deadline_s),
            write=True, required_scope="knowledge:decision:execute",
        )

    @mcp.tool()
    def inspect_systematic_review_screening_suggestion(namespace: str, candidate_id: str,
                                                        stage: str, run_id: str) -> dict:
        """Read a machine suggestion after current protocol and source checks."""
        principal_id, scopes = context()
        return safe(
            lambda conn: store(conn, initialize=False).inspect(
                namespace, candidate_id, stage, run_id,
                principal_id=principal_id, scopes=scopes),
            required_scope="knowledge:reviews:read",
        )
