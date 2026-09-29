"""Versioned free-text intent and optional hosted mode suggestion tools."""

import os


def register(mcp, safe, context):
    def intents(conn, *, initialize=True):
        from src.kb.jev_intake_routing import IntentStore
        return IntentStore(conn, initialize=initialize)

    def runtime(conn):
        from src.integrations.typesafe import TypeSafeClient
        from src.kb.decision_runtime import DecisionRuntime
        return DecisionRuntime(
            conn, client=TypeSafeClient(),
            credential_resolver=lambda reference: (
                os.environ.get("TYPESAFE_API_KEY") if reference == "typesafe" else None),
            input_resolver=intents(conn, initialize=False).resolve,
        )

    @mcp.tool()
    def register_jev_intake_intent(namespace: str, request_key: str, intent_text: str) -> dict:
        """Retain a bounded free-text intent as an owner-scoped versioned input."""
        principal_id, scopes = context()
        return safe(lambda conn: intents(conn).register(
            namespace, request_key, intent_text, principal_id=principal_id, scopes=scopes),
            write=True, required_scope="knowledge:intake:write")

    @mcp.tool()
    def revise_jev_intake_intent(namespace: str, input_id: str,
                                 expected_version: int, intent_text: str) -> dict:
        """Create a new exact intent version and invalidate older suggestions."""
        principal_id, scopes = context()
        return safe(lambda conn: intents(conn).revise(
            namespace, input_id, expected_version, intent_text,
            principal_id=principal_id, scopes=scopes),
            write=True, required_scope="knowledge:intake:write")

    @mcp.tool()
    def inspect_jev_intake_intent(namespace: str, input_id: str) -> dict:
        """Read the current owner-scoped intent version."""
        principal_id, scopes = context()
        return safe(lambda conn: intents(conn, initialize=False).inspect(
            namespace, input_id, principal_id=principal_id, scopes=scopes),
            required_scope="knowledge:intake:read")

    @mcp.tool()
    def suggest_jev_intake_route(namespace: str, run_id: str,
                                 input_id: str | None = None,
                                 answers: dict[str, bool] | None = None,
                                 override: str | None = None,
                                 policy: dict | None = None,
                                 max_cost_usd_micros: int = 0,
                                 network_approved: bool = False,
                                 max_attempts: int = 1,
                                 deadline_s: float = 30) -> dict:
        """Use explicit answers directly or suggest booleans from versioned intent."""
        from src.kb.jev_intake_routing import suggest_intake_route
        principal_id, scopes = context()
        return safe(lambda conn: suggest_intake_route(
            runtime(conn), intents(conn, initialize=False), namespace, run_id,
            principal_id=principal_id, scopes=scopes, input_id=input_id,
            answers=answers, override=override, allow_remote=network_approved,
            policy=policy, max_attempts=max_attempts,
            max_cost_usd_micros=max_cost_usd_micros, deadline_s=deadline_s),
            write=True, required_scope="knowledge:decision:execute")
