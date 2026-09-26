"""Optional Jev advice about exact retained revision pairs."""

import os


def register(mcp, safe, context):
    @mcp.tool()
    def suggest_jev_revision_significance(namespace: str, before_revision_id: str,
                                          after_revision_id: str, run_id: str,
                                          policy: dict, max_cost_usd_micros: int,
                                          network_approved: bool = False,
                                          max_attempts: int = 1,
                                          deadline_s: float = 30) -> dict:
        """Assess bounded before/after passages without changing revision facts."""
        from src.integrations.typesafe import TypeSafeClient
        from src.kb.decision_runtime import DecisionRuntime
        from src.kb.jev_revision_significance import RevisionPairResolver, suggest_revision_significance

        principal_id, scopes = context()

        def action(conn):
            resolver = RevisionPairResolver(conn)
            runtime = DecisionRuntime(
                conn, client=TypeSafeClient(), input_resolver=resolver,
                credential_resolver=lambda reference: (
                    os.environ.get("TYPESAFE_API_KEY") if reference == "typesafe" else None))
            return suggest_revision_significance(
                runtime, resolver, namespace, before_revision_id, after_revision_id,
                run_id, principal_id=principal_id, scopes=scopes,
                allow_remote=network_approved, policy=policy,
                max_cost_usd_micros=max_cost_usd_micros,
                max_attempts=max_attempts, deadline_s=deadline_s)

        return safe(action, write=True, required_scope="knowledge:decision:execute")
