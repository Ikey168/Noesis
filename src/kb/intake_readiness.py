"""Caller-scoped intake readiness without equating catalog presence with a usable journey."""

from __future__ import annotations

from typing import Any

from src.kb.intake_modes import MODES, IntakeError, IntakeStore, _text

NATIVE_TOOLS = {
    "Awareness": ["subscribe_intake_feed", "subscribe_intake_newsletter_input",
                  "ingest_intake_newsletter_message", "list_intake_feed_inbox",
                  "start_awareness_from_inbox"],
    "Exploration": ["capture_exploration_page", "suggest_exploration_sources"],
    "Decision Support": ["create_research_decision", "calculate_decision_sensitivity"],
    "Problem-Solving": ["start_problem_session", "record_problem_step"],
    "Creation": ["start_intake_creation", "command_intake_creation"],
    "Externalization": ["promote_problem_playbook", "start_guided_playbook_run"],
    "Internalization": ["create_practice_pack", "start_practice_review"],
    "Iteration": ["start_intake_iteration", "record_intake_iteration_outcome",
                  "accept_intake_playbook_revision"],
    "Maintenance": ["scan_intake_maintenance", "start_intake_maintenance"],
    "Deep Research": ["start_intake_research_topic", "inspect_research_project"],
}
MODE_SCOPES = {
    "Deep Research": ["knowledge:projects:read", "knowledge:projects:write"],
    "Decision Support": ["knowledge:decisions:read", "knowledge:decisions:write"],
    "Creation": ["knowledge:reports:read"],
}
KNOWN_GAPS = {
    "Awareness": ["A subscription count does not verify that a live feed is reachable or current; newsletter messages are caller-supplied and sender authentication is not checked"],
    "Exploration": ["Readable page acquisition requires a live fetch check and fetch scope"],
    "Deep Research": ["The complete evidence-card, claim, map, and brief workflow is not composed"],
    "Decision Support": ["Bounded evidence acquisition and task/project return links are incomplete"],
    "Problem-Solving": ["External fix execution and independent observed verification are incomplete"],
    "Creation": ["An existing authored report is required; other build adapters and independent review are incomplete"],
    "Externalization": ["Guided rehearsals are caller-reported; trusted execution is not available"],
    "Internalization": ["A source-linked author-supplied pack is required; independent mastery checks are incomplete"],
    "Iteration": ["Measured iteration currently updates Noesis playbooks only; other artifacts and Modulo-owned objects are not connected"],
    "Maintenance": ["Source-pack failures, dependency impact, and executable repair are not composed"],
}
REQUIRED_INPUTS = {
    "Awareness": ["feed_item_ids"],
    "Exploration": ["intent", "duration_minutes"],
    "Deep Research": ["questions", "success_criteria", "scope", "budget"],
    "Decision Support": ["decision_question", "options"],
    "Problem-Solving": ["symptom", "environment"],
    "Creation": ["audience", "purpose", "acceptance_criteria"],
    "Externalization": ["procedure_source", "rehearsal_criteria"],
    "Internalization": ["source_linked_cards", "schedule"],
    "Iteration": ["baseline_revision", "expected_outcome"],
    "Maintenance": ["review_scope", "health_criteria"],
}
OUTPUT_KINDS = {
    "Awareness": ["intake_item_decision"],
    "Exploration": ["exploration_source", "annotation"],
    "Deep Research": ["research_project", "research_bundle"],
    "Decision Support": ["decision"],
    "Problem-Solving": ["problem_trail", "verification"],
    "Creation": ["created_artifact"],
    "Externalization": ["procedure", "guided_run"],
    "Internalization": ["practice_pack", "attempt"],
    "Iteration": ["outcome", "revised_artifact"],
    "Maintenance": ["finding", "health_assessment"],
}


def _table(conn: Any, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [name],
    ).fetchone())


def preflight(
    conn: Any, namespace: str, *, principal_id: str,
    scopes: set[str], mode: str | None = None,
) -> dict:
    """Show what the caller may start, what is unverified, and what blocks a full journey."""
    namespace = _text(namespace, "namespace", limit=128)
    IntakeStore._authorize({"namespace": namespace, "owner": principal_id},
                           principal_id, scopes)
    if mode is not None and mode not in MODES:
        raise IntakeError("invalid_mode", "preflight mode must name one of the ten modes")
    subscriptions = 0
    if _table(conn, "intake_inbox_subscriptions"):
        subscriptions = int(conn.execute(
            "SELECT count(*) FROM intake_inbox_subscriptions "
            "WHERE namespace=? AND owner=? AND enabled=true",
            [namespace, principal_id],
        ).fetchone()[0])
    ledger_initialized = _table(conn, "intake_sessions")
    operator = "operator" in scopes
    results = []
    for name in ([mode] if mode else MODES):
        required = ["knowledge:intake:read", "knowledge:intake:write",
                    f"namespace:{namespace}:read", f"namespace:{namespace}:write",
                    *MODE_SCOPES.get(name, [])]
        missing = [] if operator else [scope for scope in required if scope not in scopes]
        blockers = [*KNOWN_GAPS[name]]
        if name == "Exploration" and not operator and "knowledge:intake:fetch" not in scopes:
            blockers.insert(0, "Readable page acquisition needs knowledge:intake:fetch scope")
        if missing:
            blockers.insert(0, "Missing required scopes: " + ", ".join(missing))
        if name == "Awareness" and subscriptions == 0:
            blockers.insert(0, "No enabled feed subscription in this namespace; subscribe and refresh")
        results.append({
            "mode": name,
            "native_tools": NATIVE_TOOLS[name],
            "required_scopes": required,
            "missing_scopes": missing,
            "native_start_possible": bool(NATIVE_TOOLS[name]) and not missing
            and (name != "Awareness" or subscriptions > 0),
            "complete_journey_ready": False,
            "blockers": blockers,
            "live_source_verified": False,
            "fetch_scope_available": operator or "knowledge:intake:fetch" in scopes,
            "model_readiness": "not_checked" if name == "Deep Research" else "not_required_by_listed_tools",
            "execution_readiness": "not_available" if name == "Externalization" else "not_checked",
        })
    return {
        "contract": "noesis-intake-readiness-v1",
        "namespace": namespace,
        "owner": principal_id,
        "transport_accessible": True,
        "ledger_initialized": ledger_initialized,
        "enabled_feed_subscription_count": subscriptions,
        "source_mode": "unknown_live_or_fixture",
        "delivery_readiness": "not_checked",
        "modes": results,
    }


def discover_workflows(
    conn: Any, namespace: str, *, principal_id: str, scopes: set[str],
    mode: str | None = None,
) -> dict:
    """Return caller-scoped operations and durable next commands for each mode."""
    readiness = preflight(
        conn, namespace, principal_id=principal_id, scopes=scopes, mode=mode)
    rows = []
    if readiness["ledger_initialized"]:
        rows = conn.execute(
            "SELECT session_id,mode,status,revision FROM intake_sessions "
            "WHERE namespace=? AND owner=? ORDER BY session_id LIMIT 100",
            [namespace, principal_id],
        ).fetchall()
    sessions = []
    for session_id, session_mode, status, revision in rows:
        if mode is not None and session_mode != mode:
            continue
        allowed = ["inspect", "export"]
        if status == "active" and ("operator" in scopes or (
            "knowledge:intake:write" in scopes and f"namespace:{namespace}:write" in scopes)):
            allowed += ["record", "pause", "complete", "cancel"]
        elif status == "paused" and ("operator" in scopes or (
            "knowledge:intake:write" in scopes and f"namespace:{namespace}:write" in scopes)):
            allowed += ["resume", "cancel"]
        sessions.append({
            "session_id": session_id, "mode": session_mode,
            "status": status, "revision": int(revision),
            "resource": f"noesis://intake/{namespace}/{session_id}",
            "allowed_next_actions": allowed,
        })
    modes = []
    for item in readiness["modes"]:
        name = item["mode"]
        modes.append({
            **item, "required_inputs": REQUIRED_INPUTS[name],
            "output_kinds": OUTPUT_KINDS[name],
            "allowed_mutations": ["start_intake_mode"] if item["native_start_possible"] else [],
            "artifact_resource_template": "noesis://intake/{namespace}/{session_id}",
            "readiness_basis": "caller_scopes_and_local_state_only",
        })
    return {
        "contract": "noesis-intake-workflow-discovery-v1",
        "namespace": namespace, "owner": principal_id,
        "source_mode": readiness["source_mode"],
        "live_source_verified": False,
        "delivery_readiness": readiness["delivery_readiness"],
        "modes": modes, "sessions": sessions[:100],
    }
