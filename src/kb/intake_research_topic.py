"""Atomic Deep Research topic handoff to the authoritative research project."""

from __future__ import annotations

from typing import Any

from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_inbox import IntakeInboxStore
from src.kb.intake_modes import IntakeStore, _reference
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
    inherited = []
    if origin:
        parent = intake._state(namespace, origin["session_id"])
        intake._authorize_full_read(parent, principal_id, scopes)
        inherited = list(parent["references"])
    normalized_refs = []
    for raw in [*inherited, *references]:
        ref = _reference(raw, namespace, scopes)
        if ref not in normalized_refs:
            normalized_refs.append(ref)
    # Create source tables before opening the shared transaction: DuckDB DDL
    # cannot be interleaved with the handoff's writes on this connection.
    exploration = IntakeExplorationStore(conn) if any(
        ref["kind"] == "exploration_source" for ref in normalized_refs
    ) else None
    inbox = IntakeInboxStore(conn) if any(
        ref["kind"] == "intake_feed_item" for ref in normalized_refs
    ) else None
    conn.execute("BEGIN")
    try:
        initial_links = []
        for ref in normalized_refs:
            if ref["kind"] == "exploration_source":
                assert exploration is not None
                exploration.inspect_source(
                    ref["namespace"], ref["id"], version=ref["version"],
                    principal_id=principal_id, scopes=scopes,
                )
            elif ref["kind"] == "intake_feed_item":
                assert inbox is not None
                inbox.inspect(
                    ref["namespace"], ref["id"], revision=ref["version"],
                    principal_id=principal_id, scopes=scopes,
                )
            else:
                continue
            # Resolve the exact historical source before pinning its identity.
            link = {"kind": "intake_source", "id": ref["id"],
                    "namespace": ref["namespace"], "revision": ref["version"]}
            if link not in initial_links:
                initial_links.append(link)
        project = projects.create(
            namespace, request_key, questions=questions,
            success_criteria=success_criteria, scope=scope, budget=budget,
            principal_id=principal_id, scopes=scopes,
            _within_transaction=True,
            _initial_links=initial_links,
        )
        session = intake.create(
            namespace, "Deep Research", request_key,
            intent=questions[0],
            inputs={"research_project_id": project["project_id"],
                    "research_project_revision": 1,
                    "definition_of_done": success_criteria,
                    "research_budget": budget},
            origin=origin, references=[*normalized_refs, {
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
