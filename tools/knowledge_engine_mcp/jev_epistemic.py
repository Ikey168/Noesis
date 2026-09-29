"""Optional source-grounded hosted statement-kind suggestions."""

import os


def register(mcp, safe, context):
    @mcp.tool()
    def suggest_jev_epistemic_kind(
        namespace: str,
        statement_id: str,
        statement: str,
        run_id: str,
        source_refs: list[dict],
        policy: dict,
        max_cost_usd_micros: int,
        network_approved: bool = False,
        max_attempts: int = 1,
        deadline_s: float = 20,
    ) -> dict:
        """Classify statement form from a verbatim span in an authorized version."""
        from src.integrations.typesafe import TypeSafeClient
        from src.kb.decision_runtime import DecisionRuntime
        from src.kb.epistemic_decisions import classify_with_decision

        principal_id, scopes = context()

        def action(conn):
            runtime = DecisionRuntime(
                conn,
                client=TypeSafeClient(),
                credential_resolver=lambda reference: (
                    os.environ.get("TYPESAFE_API_KEY")
                    if reference == "typesafe"
                    else None
                ),
            )
            result = classify_with_decision(
                runtime,
                namespace,
                statement_id,
                statement,
                run_id,
                source_refs=source_refs,
                principal_id=principal_id,
                scopes=scopes,
                allow_remote=network_approved,
                policy=policy,
                max_attempts=max_attempts,
                max_cost_usd_micros=max_cost_usd_micros,
                deadline_s=deadline_s,
            )
            run = result.get("decision_run") or {}
            return {
                "contract": "noesis-jev-epistemic-kind-suggestion-v1",
                "status": "suggested"
                if run.get("status") == "completed"
                and run.get("rollout_mode") != "shadow"
                and result.get("selected_probability") is not None
                else "unavailable",
                "accepted": False,
                "statement_id": statement_id,
                "kind": result["status"],
                "confidence": result["confidence"],
                "selected_probability": result.get("selected_probability"),
                "vendor_confidence": result.get("vendor_confidence"),
                "signals": result.get("signals", []),
                "source_binding": run.get("source_binding", []),
                "decision_run": run,
                "truth_verified": False,
            }

        return safe(action, write=True, required_scope="knowledge:decision:execute")
