"""Atomic Deep Research topic handoff to the authoritative research project."""

from __future__ import annotations

from typing import Any

from src.kb.intake_modes import IntakeStore
from src.kb.research_projects import ResearchProjectStore


def start_research_topic(
    conn: Any, namespace: str, request_key: str, *,
    questions: list[str], success_criteria: list[str],
    scope: dict[str, Any], budget: dict[str, int],
    origin: dict[str, Any] | None, references: list[dict[str, Any]],
    workspace_links: list[dict[str, Any]] | None,
    principal_id: str, scopes: set[str],
) -> dict[str, Any]:
    """Create or replay both records; a failed capacity check leaves neither."""
    projects = ResearchProjectStore(conn)
    intake = IntakeStore(conn)
    conn.execute("BEGIN")
    try:
        project = projects.create(
            namespace, request_key, questions=questions,
            success_criteria=success_criteria, scope=scope, budget=budget,
            principal_id=principal_id, scopes=scopes,
            _within_transaction=True,
        )
        session = intake.create(
            namespace, "Deep Research", request_key,
            intent=questions[0],
            inputs={"research_project_id": project["project_id"],
                    "research_project_revision": 1,
                    "definition_of_done": success_criteria,
                    "research_budget": budget},
            origin=origin, references=[*references, {
                "kind": "research_project", "id": project["project_id"],
                "namespace": namespace, "version": 1,
            }], workspace_links=workspace_links,
            principal_id=principal_id, scopes=scopes,
            _within_transaction=True,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"contract": "noesis-intake-research-topic-v1",
            "project": project, "session": session,
            "idempotent": project["idempotent"] and session["idempotent"]}
