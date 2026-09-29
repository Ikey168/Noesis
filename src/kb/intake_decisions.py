"""Source-bound semantic suggestions for an Awareness inbox session."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.kb.intake_inbox import IntakeInboxStore
from src.kb.intake_modes import DECISIONS, IntakeError, IntakeStore

CONTRACT = "noesis-awareness-decision-suggestion-v1"
TASK = "awareness-triage-v1"
MAX_CONTENT_CHARS = 12_000
MAX_COMPARISONS = 5


def awareness_questions(*, compare_novelty: bool) -> dict[str, dict[str, Any]]:
    """Ask related judgments over one bounded item in a single request."""
    questions: dict[str, dict[str, Any]] = {
        "relevance": {
            "type": "noul",
            "instructions": "Is this feed item substantively relevant to the stated Awareness objective? Judge its content, not keyword overlap.",
            "criteria": {"true": "Substantively relevant", "false": "Not substantively relevant"},
        },
        "urgency": {
            "type": "score",
            "instructions": "How soon does this item require the user's attention for the stated objective?",
            "criteria": ["No time sensitivity", "Can wait for routine review", "Time sensitive", "Immediate attention warranted"],
        },
        "advice": {
            "type": "choice",
            "instructions": "Which single inbox action is most useful for the stated objective? Base the choice on the item text. Do not assume an allegation is verified.",
            "criteria": {
                "watch": "Keep this item in the current stream",
                "escalate": "Promote this item to deeper investigation",
                "schedule": "Set it aside for later deliberate review",
                "discard": "No useful connection to the objective",
                "archive": "Retain as background without active tracking",
                "flag": "Needs human review because relevance or reliability is unclear",
            },
        },
    }
    if compare_novelty:
        questions["novelty"] = {
            "type": "noul",
            "instructions": "Does the current item add a substantive new development relative only to the supplied comparison item versions?",
            "criteria": {"true": "New substantive development", "false": "No substantive new development"},
        }
    return questions


def _answer(receipt: Mapping[str, Any], question_id: str) -> dict[str, Any] | None:
    answer = receipt.get("answers", {}).get(question_id)
    if not isinstance(answer, Mapping) or answer.get("status") != "answered":
        return None
    return dict(answer)


def _excerpt(item: Mapping[str, Any]) -> dict[str, Any]:
    """A literal source excerpt, never a model-authored explanation."""
    content = str(item["content"])
    if content:
        return {"field": "content", "start": 0, "end": min(len(content), 280), "text": content[:280]}
    title = str(item["title"])
    return {"field": "title", "start": 0, "end": min(len(title), 280), "text": title[:280]}


def suggest_awareness_item(
    runtime: Any,
    inbox: IntakeInboxStore,
    namespace: str,
    session_id: str,
    item_id: str,
    run_id: str,
    *,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    comparison_items: Sequence[Mapping[str, Any]] = (),
    max_attempts: int = 1,
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 20.0,
) -> dict[str, Any]:
    """Preview advice without changing inbox or session state.

    Comparison items are exact ``item_id``/``source_version`` pairs. Without
    them novelty remains unknown, rather than being inferred from no context.
    """
    session = IntakeStore(inbox.conn).inspect(
        namespace, session_id, principal_id=principal_id, scopes=scopes
    )
    if session["mode"] != "Awareness" or item_id not in session["inputs"]["feed_item_ids"]:
        raise IntakeError("item_not_in_queue", "item is outside this Awareness queue")
    item = inbox.inspect(namespace, item_id, principal_id=principal_id, scopes=scopes)
    if item["decision"] is not None:
        raise IntakeError("item_already_triaged", "item already has an inbox decision")
    if len(item["content"]) > MAX_CONTENT_CHARS:
        raise IntakeError("item_too_long", "item exceeds the bounded semantic preview size")
    if len(comparison_items) > MAX_COMPARISONS:
        raise IntakeError("too_many_comparisons", "at most five comparison items are supported")
    comparison_state: list[dict[str, Any]] = []
    refs = [item["reference"]]
    runtime_refs = [{"item_id": item_id, "source_version": item["source_version"]}]
    seen = {item_id}
    for spec in comparison_items:
        candidate_id = str(spec.get("item_id") or "")
        version = spec.get("source_version")
        if not candidate_id or candidate_id in seen or type(version) is not int:
            raise IntakeError("invalid_comparison", "comparison item identity and version are required")
        seen.add(candidate_id)
        candidate = inbox.inspect(
            namespace, candidate_id, principal_id=principal_id, scopes=scopes
        )
        if candidate["source_version"] != version:
            raise IntakeError("source_changed", "comparison item version has changed")
        if len(candidate["content"]) > MAX_CONTENT_CHARS:
            raise IntakeError("item_too_long", "comparison item exceeds the bounded preview size")
        refs.append(candidate["reference"])
        runtime_refs.append({"item_id": candidate_id, "source_version": version})
        comparison_state.append({
            "item_id": candidate_id,
            "source_version": version,
            "title": candidate["title"],
            "content": candidate["content"],
        })
    state = {
        "objective": session["intent"],
        "item": {"title": item["title"], "content": item["content"]},
        "comparison_items": comparison_state,
    }
    result = runtime.run(
        namespace,
        run_id,
        TASK,
        state=state,
        questions=awareness_questions(compare_novelty=bool(comparison_state)),
        source_refs=runtime_refs,
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=dict(policy),
        max_attempts=max_attempts,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
    )
    current = inbox.inspect(namespace, item_id, principal_id=principal_id, scopes=scopes)
    if current["source_version"] != item["source_version"] or current["decision"] is not None:
        raise IntakeError("source_changed", "item changed during semantic preview")
    for spec in comparison_items:
        current_comparison = inbox.inspect(
            namespace, str(spec["item_id"]), principal_id=principal_id, scopes=scopes
        )
        if current_comparison["source_version"] != spec["source_version"]:
            raise IntakeError("source_changed", "comparison item changed during semantic preview")
    receipt = result.get("receipt") or {}
    advice = _answer(receipt, "advice")
    relevance = _answer(receipt, "relevance")
    urgency = _answer(receipt, "urgency")
    novelty = _answer(receipt, "novelty") if comparison_state else None
    shadow = result.get("rollout_mode") == "shadow"
    complete = result.get("status") == "completed" and not shadow and all((advice, relevance, urgency))
    action = advice.get("value") if complete and advice else None
    if action not in DECISIONS:
        action = None
    reasons = ["semantic_relevance_evaluated", "urgency_evaluated"] if complete else [
        "shadow_mode" if shadow else "semantic_decision_unavailable"
    ]
    if action == "discard":
        decision_policy = result.get("decision_policy") or {}
        threshold = decision_policy.get("discard_max_relevance_p")
        probability = relevance.get("value") if relevance else None
        calibrated = (
            isinstance(decision_policy.get("calibration_id"), str)
            and bool(decision_policy["calibration_id"])
            and type(threshold) in (int, float)
            and not isinstance(threshold, bool)
            and 0 <= threshold <= 1
            and type(probability) in (int, float)
            and not isinstance(probability, bool)
            and 0 <= probability <= threshold
        )
        if not calibrated:
            action = None
            reasons.append("discard_requires_calibrated_relevance")
    if comparison_state:
        reasons.append("novelty_evaluated" if novelty else "novelty_unknown")
    else:
        reasons.append("novelty_unknown_no_comparison")
    return {
        "contract": CONTRACT,
        "status": "suggested" if action else "unavailable",
        "task": TASK,
        "session_id": session_id,
        "session_revision": session["revision"],
        "run_id": run_id,
        "item_id": item_id,
        "source_reference": item["reference"],
        "comparison_references": refs[1:],
        "suggested_decision": action,
        "relevance": relevance,
        "urgency": urgency,
        "novelty": novelty or {"status": "unknown"},
        "reason_codes": reasons,
        "source_excerpt": _excerpt(item),
        "decision_run": result,
    }


def accept_awareness_suggestion(
    inbox: IntakeInboxStore,
    namespace: str,
    suggestion: Mapping[str, Any],
    command_key: str,
    *,
    principal_id: str,
    scopes: set[str],
) -> dict[str, Any]:
    """Apply an explicitly accepted, still-current suggestion via the ledger."""
    if (
        suggestion.get("contract") != CONTRACT
        or suggestion.get("task") != TASK
        or suggestion.get("status") != "suggested"
    ):
        raise IntakeError("invalid_suggestion", "only a completed suggestion can be accepted")
    decision = suggestion.get("suggested_decision")
    if decision not in DECISIONS:
        raise IntakeError("invalid_suggestion", "suggestion has no valid inbox decision")
    run = suggestion.get("decision_run") or {}
    receipt = run.get("receipt") or {}
    answer = receipt.get("answers", {}).get("advice") or {}
    if (
        run.get("status") != "completed"
        or run.get("rollout_mode") == "shadow"
        or receipt.get("status") != "answered"
        or answer.get("status") != "answered"
        or answer.get("value") != decision
    ):
        raise IntakeError("invalid_suggestion", "suggestion lacks a completed matching decision")
    if decision == "discard":
        rule = run.get("decision_policy") or {}
        probability = (receipt.get("answers", {}).get("relevance") or {}).get("value")
        threshold = rule.get("discard_max_relevance_p")
        if (
            not isinstance(rule.get("calibration_id"), str)
            or not rule["calibration_id"]
            or type(probability) not in {int, float}
            or type(threshold) not in {int, float}
            or not 0 <= probability <= threshold <= 1
        ):
            raise IntakeError("invalid_suggestion", "discard lacks a calibrated relevance gate")
    reference = suggestion.get("source_reference") or {}
    item_id = suggestion.get("item_id")
    item = inbox.inspect(namespace, item_id, principal_id=principal_id, scopes=scopes)
    if item["source_version"] != reference.get("version") or item["decision"] is not None:
        raise IntakeError("source_changed", "item changed after semantic preview")
    for ref in suggestion.get("comparison_references") or []:
        candidate = inbox.inspect(namespace, ref["id"], principal_id=principal_id, scopes=scopes)
        if candidate["source_version"] != ref["version"]:
            raise IntakeError("source_changed", "comparison item changed after semantic preview")
    return inbox.triage_awareness(
        namespace,
        suggestion["session_id"],
        item_id,
        command_key,
        expected_revision=suggestion["session_revision"],
        decision=decision,
        principal_id=principal_id,
        scopes=scopes,
    )
