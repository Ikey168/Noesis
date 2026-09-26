"""Explicit hosted typed-decision execution on authorized captured sources."""

from __future__ import annotations

import os


def register(mcp, safe, context):
    def runtime(conn, *, initialize=True):
        from src.integrations.typesafe import TypeSafeClient
        from src.kb.decision_runtime import DecisionRuntime

        return DecisionRuntime(
            conn,
            client=TypeSafeClient(),
            initialize=initialize,
            credential_resolver=lambda reference: (
                os.environ.get("TYPESAFE_API_KEY") if reference == "typesafe" else None
            ),
        )

    @mcp.tool()
    def configure_jev_task_rollout(
        namespace: str, task: str, mode: str,
        model: str | None = None, rubric_id: str | None = None,
        evaluation_ref: str | None = None,
    ) -> dict:
        """Set an authorized off, shadow or suggestion mode for one pinned task."""
        return safe(
            lambda conn: runtime(conn).configure_task_rollout(
                namespace, task, mode, model=model, rubric_id=rubric_id,
                evaluation_ref=evaluation_ref, principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True, required_scope="knowledge:decision:configure",
        )

    @mcp.tool()
    def inspect_jev_task_rollout(namespace: str, task: str) -> dict:
        """Read the current task mode and evidence reference without credentials."""
        return safe(
            lambda conn: runtime(conn, initialize=False).inspect_task_rollout(
                namespace, task, principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:decision:read",
        )

    @mcp.tool()
    def run_hosted_typed_decision(
        namespace: str,
        run_id: str,
        task: str,
        state: dict,
        questions: dict,
        sources: list[dict],
        policy: dict,
        max_cost_usd_micros: int,
        network_approved: bool = False,
        max_attempts: int = 1,
        deadline_s: float = 30,
    ) -> dict:
        """Evaluate source-bound questions through the optional hosted provider."""
        return safe(
            lambda conn: runtime(conn).run(
                namespace,
                run_id,
                task,
                state=state,
                questions=questions,
                source_refs=sources,
                principal_id=context()[0],
                scopes=context()[1],
                allow_remote=network_approved,
                policy=policy,
                max_attempts=max_attempts,
                max_cost_usd_micros=max_cost_usd_micros,
                deadline_s=deadline_s,
            ),
            write=True,
            required_scope="knowledge:decision:execute",
        )

    @mcp.tool()
    def inspect_hosted_typed_decision(namespace: str, run_id: str) -> dict:
        """Inspect a durable run after current source access is rechecked."""
        return safe(
            lambda conn: runtime(conn, initialize=False).inspect(
                namespace,
                run_id,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=False,
            required_scope="knowledge:decision:read",
        )

    @mcp.tool()
    def suggest_jev_task(
        namespace: str,
        run_id: str,
        task: str,
        parameters: dict,
        sources: list[dict],
        policy: dict,
        max_cost_usd_micros: int,
        network_approved: bool = False,
        max_attempts: int = 1,
        deadline_s: float = 30,
    ) -> dict:
        """Run a bounded task-specific Jev plan and return a suggestion only."""
        from src.kb.jev_tasks import suggest_task

        return safe(
            lambda conn: suggest_task(
                runtime(conn), namespace, run_id, task, parameters, sources,
                principal_id=context()[0], scopes=context()[1],
                allow_remote=network_approved, policy=policy,
                max_attempts=max_attempts,
                max_cost_usd_micros=max_cost_usd_micros,
                deadline_s=deadline_s,
            ),
            write=True,
            required_scope="knowledge:decision:execute",
        )

    @mcp.tool()
    def suggest_awareness_with_jev(
        namespace: str,
        session_id: str,
        item_id: str,
        run_id: str,
        policy: dict,
        max_cost_usd_micros: int,
        comparison_items: list[dict] | None = None,
        network_approved: bool = False,
    ) -> dict:
        """Suggest a feed-item action with source-version and session binding."""
        from src.kb.intake_decisions import suggest_awareness_item
        from src.kb.intake_inbox import IntakeInboxStore

        return safe(
            lambda conn: suggest_awareness_item(
                runtime(conn),
                IntakeInboxStore(conn),
                namespace,
                session_id,
                item_id,
                run_id,
                principal_id=context()[0],
                scopes=context()[1],
                allow_remote=network_approved,
                policy=policy,
                comparison_items=comparison_items or (),
                max_cost_usd_micros=max_cost_usd_micros,
            ),
            write=True,
            required_scope="knowledge:decision:execute",
        )

    @mcp.tool()
    def accept_awareness_jev_suggestion(
        namespace: str,
        suggestion: dict,
        command_key: str,
    ) -> dict:
        """Apply an accepted suggestion through the existing atomic triage command."""
        from src.kb.intake_decisions import accept_awareness_suggestion
        from src.kb.intake_decisions import TASK as awareness_task
        from src.kb.intake_inbox import IntakeInboxStore

        def accept(conn):
            # The submitted suggestion is untrusted MCP input. Re-read its
            # durable run before allowing it to influence an inbox command.
            stored = runtime(conn).inspect(
                namespace,
                suggestion.get("run_id", ""),
                principal_id=context()[0],
                scopes=context()[1],
            )
            supplied = suggestion.get("decision_run") or {}
            rollout = runtime(conn, initialize=False).inspect_task_rollout(
                namespace, awareness_task,
                principal_id=context()[0], scopes=context()[1],
            )
            if (
                stored.get("status") != "completed"
                or stored.get("rollout_mode") == "shadow"
                or (rollout["configured_by"] is not None and rollout["mode"] != "suggestion")
                or supplied.get("receipt") != stored.get("receipt")
                or supplied.get("source_binding") != stored.get("source_binding")
                or supplied.get("decision_policy") != stored.get("decision_policy")
            ):
                raise ValueError("suggestion does not match the stored hosted decision")
            bindings = stored.get("source_binding") or []
            reference = suggestion.get("source_reference") or {}
            if (
                not bindings
                or bindings[0].get("kind") != "inbox_item_version"
                or bindings[0].get("item_id") != suggestion.get("item_id")
                or bindings[0].get("source_version") != reference.get("version")
                or reference.get("id") != suggestion.get("item_id")
                or [
                    (binding.get("item_id"), binding.get("source_version"))
                    for binding in bindings[1:]
                ]
                != [
                    (ref.get("id"), ref.get("version"))
                    for ref in suggestion.get("comparison_references", [])
                ]
            ):
                raise ValueError("suggestion source references do not match the stored run")
            return accept_awareness_suggestion(
                IntakeInboxStore(conn),
                namespace,
                suggestion,
                command_key,
                principal_id=context()[0],
                scopes=context()[1],
            )

        return safe(
            accept,
            write=True,
            required_scope="knowledge:decision:read",
        )
