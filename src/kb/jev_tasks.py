"""Bounded Jev question plans for optional, source-bound Noesis judgments.

These plans produce suggestions only. Domain stores continue to own publication,
review votes, graph links and accepted decisions. Each plan receives captured
source text from DecisionRuntime rather than caller-supplied source prose.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


class JevTaskError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


TASKS = frozenset(
    {
        "stance",
        "frames",
        "screen_abstract",
        "screen_fulltext",
        "reranking",
        "answer_support",
        "claim_links",
        "intake_routing",
        "review_priority",
        "methodology",
        "entity_matching",
        "source_matching",
        "revision_significance",
        "source_selection",
        "claim_detection",
        "checkworthiness",
        "sentiment",
        "attribution",
    }
)


def _text(value: Any, name: str, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise JevTaskError(
            "invalid_task_input", f"{name} must be nonempty bounded text"
        )
    return value.strip()


def _options(
    value: Any, name: str, *, maximum: int = 20, minimum: int = 2
) -> dict[str, str]:
    if not isinstance(value, Mapping) or not minimum <= len(value) <= maximum:
        raise JevTaskError(
            "invalid_task_input", f"{name} needs {minimum}–{maximum} options"
        )
    result = {}
    for key, description in value.items():
        label = _text(key, "option label", 100)
        result[label] = _text(description, "option description", 500)
    if len(result) != len(value):
        raise JevTaskError("invalid_task_input", "duplicate normalized options")
    return result


def _list(value: Any, name: str, *, maximum: int = 20) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not 1 <= len(value) <= maximum
    ):
        raise JevTaskError(
            "invalid_task_input", f"{name} needs one to {maximum} entries"
        )
    return [_text(item, name, 500) for item in value]


def _choice(instructions: str, criteria: Mapping[str, str]) -> dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def _noul(instructions: str) -> dict[str, Any]:
    return {"type": "noul", "instructions": instructions}


def _score(instructions: str, levels: Sequence[str]) -> dict[str, Any]:
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def prepare_task(
    task: str, parameters: Mapping[str, Any], *, source_count: int
) -> tuple[dict, dict]:
    """Return structured state and atomic questions for one bounded task.

    ``source_count`` refers to exact versions supplied to DecisionRuntime. The
    runtime injects their text at ``state.sources`` after authorization.
    """
    if (
        task not in TASKS
        or not isinstance(parameters, Mapping)
        or "sources" in parameters
    ):
        raise JevTaskError(
            "invalid_task", "supported task and structured parameters required"
        )
    if not 1 <= source_count <= 20:
        raise JevTaskError(
            "invalid_sources", "one to 20 exact source versions required"
        )
    state: dict[str, Any] = {}
    questions: dict[str, dict[str, Any]] = {}

    if task == "stance":
        state = {
            "topic": _text(parameters.get("topic"), "topic"),
            "sentence_index": parameters.get("sentence_index"),
        }
        if type(state["sentence_index"]) is not int or state["sentence_index"] < 0:
            raise JevTaskError(
                "invalid_task_input", "nonnegative sentence index required"
            )
        if "sentence_text" in parameters:
            state["sentence_text"] = _text(
                parameters["sentence_text"], "sentence", 4000
            )
            span = parameters.get("sentence_span")
            if (
                not isinstance(span, (list, tuple))
                or len(span) != 2
                or any(type(value) is not int or value < 0 for value in span)
                or span[0] >= span[1]
            ):
                raise JevTaskError(
                    "invalid_task_input", "valid sentence character span required"
                )
            state["sentence_span"] = list(span)
            state["bounded_context"] = _text(
                parameters.get("bounded_context"), "sentence context", 8000
            )
        questions["stance"] = _choice(
            "How does the cited sentence express a position toward `topic`? A quotation may report someone else's position; use ambiguous when no clear position is expressed.",
            {
                "supportive": "Supports the topic or proposition",
                "critical": "Opposes or criticizes it",
                "neutral": "Describes it without a position",
                "ambiguous": "Mixed, unclear, or insufficient context",
            },
        )
    elif task == "frames":
        labels = _options(parameters.get("frames"), "frames", maximum=16)
        state = {
            "document_kind": _text(
                parameters.get("document_kind"), "document kind", 100
            )
        }
        if "window_text" in parameters:
            state["window_text"] = _text(
                parameters["window_text"], "evidence window", 6000
            )
            span = parameters.get("window_span")
            if (
                not isinstance(span, (list, tuple))
                or len(span) != 2
                or any(type(value) is not int or value < 0 for value in span)
                or span[0] >= span[1]
            ):
                raise JevTaskError(
                    "invalid_task_input", "valid window character span required"
                )
            state["window_span"] = list(span)
        for index, (label, description) in enumerate(labels.items()):
            questions[f"frame_{index}"] = _noul(
                f"Does this evidence window substantively use the {label} frame, meaning: {description}? Consider the supplied window; a document can use multiple frames."
            )
        state["frame_labels"] = list(labels)
    elif task in {"screen_abstract", "screen_fulltext"}:
        criteria = _list(parameters.get("criteria"), "protocol criteria", maximum=20)
        state = {
            "protocol_id": _text(parameters.get("protocol_id"), "protocol ID", 200),
            "protocol_revision": parameters.get("protocol_revision"),
            "stage": task,
        }
        if (
            type(state["protocol_revision"]) is not int
            or state["protocol_revision"] < 1
        ):
            raise JevTaskError(
                "invalid_task_input", "positive protocol revision required"
            )
        for index, criterion in enumerate(criteria):
            questions[f"criterion_{index}"] = _choice(
                f"At this screening stage, does the captured candidate explicitly satisfy protocol criterion {index + 1}: {criterion}? Choose not_reported when the required fact cannot be found in the supplied source.",
                {
                    "satisfied": "Evidence explicitly supports this criterion",
                    "not_satisfied": "Evidence explicitly conflicts with this criterion",
                    "not_reported": "The supplied source does not establish either outcome",
                },
            )
        state["criteria"] = criteria
    elif task == "reranking":
        state = {
            "query": _text(parameters.get("query"), "query"),
            "candidate_count": source_count,
        }
        trace = parameters.get("candidate_trace")
        if trace is not None:
            if (
                not isinstance(trace, Sequence)
                or isinstance(trace, (str, bytes))
                or len(trace) != source_count
            ):
                raise JevTaskError(
                    "invalid_task_input", "candidate trace must align with the exact shortlist"
                )
            normalized = []
            for index, item in enumerate(trace):
                score = item.get("original_score") if isinstance(item, Mapping) else None
                if (
                    not isinstance(item, Mapping)
                    or set(item) != {"original_index", "original_score"}
                    or type(item["original_index"]) is not int
                    or item["original_index"] != index
                    or type(score) not in {int, float}
                    or not math.isfinite(score)
                ):
                    raise JevTaskError(
                        "invalid_task_input", "candidate indices and finite original scores required"
                    )
                normalized.append(
                    {"original_index": index, "original_score": float(score)}
                )
            state["candidate_trace"] = normalized
        for index in range(source_count):
            questions[f"relevant_{index}"] = _noul(
                f"Does `sources[{index}].content` directly address `query`, rather than merely sharing keywords?"
            )
            questions[f"answer_bearing_{index}"] = _noul(
                f"Does `sources[{index}].content` contain information that could answer `query` with a citation?"
            )
    elif task == "answer_support":
        if source_count != 1:
            raise JevTaskError(
                "invalid_sources", "answer support needs one exact cited passage"
            )
        state = {
            "statement": _text(parameters.get("statement"), "statement", 8000),
            "locator": dict(parameters.get("locator") or {}),
        }
        if "passage_text" in parameters:
            state["passage_text"] = _text(
                parameters["passage_text"], "cited passage", 8000
            )
        questions["relation"] = _choice(
            "Does the exact cited passage support or contradict `statement`? Judge only supplied text. A related topic alone is not support.",
            {
                "entailment": "The cited passage directly supports the statement",
                "contradiction": "The cited passage conflicts with the statement",
                "neutral": "The passage is related but does not establish the statement",
                "unavailable": "The passage lacks enough evidence to judge",
            },
        )
    elif task == "claim_links":
        if source_count != 2:
            raise JevTaskError(
                "invalid_sources", "claim relation needs two exact claim sources"
            )
        state = {
            "claim_a": _text(parameters.get("claim_a"), "claim A", 8000),
            "claim_b": _text(parameters.get("claim_b"), "claim B", 8000),
        }
        relation = {
            "entailment": "Premise entails the hypothesis",
            "contradiction": "Premise contradicts the hypothesis",
            "neutral": "Neither relation is established",
        }
        questions["a_to_b"] = _choice(
            "Does `claim_a` entail, contradict, or have no established relation to `claim_b`?",
            relation,
        )
        questions["b_to_a"] = _choice(
            "Does `claim_b` entail, contradict, or have no established relation to `claim_a`?",
            relation,
        )
    elif task == "intake_routing":
        state = {"intent": _text(parameters.get("intent"), "intent", 8000)}
        modes = _options(parameters.get("modes"), "intake modes", maximum=10)
        questions["mode"] = _choice(
            "Which workflow mode best fits the user's stated intent? Preserve explicit user choices when present.",
            modes,
        )
    elif task == "review_priority":
        state = {
            "review_goal": _text(parameters.get("review_goal"), "review goal"),
            "current_uncertainty": parameters.get("current_uncertainty"),
        }
        questions["impact"] = _score(
            "How consequential would an error in the captured item be for the stated review goal?",
            ["Negligible", "Limited", "Material", "Critical"],
        )
    elif task == "methodology":
        designs = _options(parameters.get("study_designs"), "study designs", maximum=20)
        state = {"study_id": _text(parameters.get("study_id"), "study ID", 200)}
        questions["study_design"] = _choice(
            "Which study design is explicitly described by the captured study passage? Choose unknown if none is established.",
            {**designs, "unknown": "No design can be identified from the passage"},
        )
        questions["limitation_present"] = _noul(
            "Does the captured passage explicitly identify a study limitation?"
        )
    elif task in {"entity_matching", "source_matching"}:
        candidates = _options(
            parameters.get("candidates"), "identity candidates", maximum=20, minimum=1
        )
        state = {
            "reference_identity": _text(
                parameters.get("reference_identity"), "reference identity", 1000
            ),
            "candidate_ids": list(candidates),
        }
        questions["match"] = _choice(
            "Which supplied candidate refers to the same real-world identity? Choose none when none is established and uncertain when evidence is ambiguous.",
            {
                **candidates,
                "none": "No listed candidate matches",
                "uncertain": "Insufficient evidence to choose",
            },
        )
    elif task == "revision_significance":
        if source_count != 2:
            raise JevTaskError(
                "invalid_sources",
                "revision comparison needs before and after source versions",
            )
        state = {"before_index": 0, "after_index": 1}
        questions["significance"] = _score(
            "How much did the meaning of the relevant claim change from `sources[0]` to `sources[1]`?",
            [
                "Cosmetic only",
                "Minor nuance",
                "Material claim change",
                "Reversal or retraction",
            ],
        )
        questions["claim_changed"] = _noul(
            "Does the before/after pair change a factual claim, rather than wording alone?"
        )
    elif task == "source_selection":
        eligible = _options(
            parameters.get("eligible_sources"), "eligible sources", maximum=20
        )
        state = {
            "objective": _text(parameters.get("objective"), "objective"),
            "eligible_source_ids": list(eligible),
        }
        for index, (source_id, description) in enumerate(eligible.items()):
            questions[f"eligible_{index}"] = _score(
                f"How relevant is eligible source {source_id} ({description}) to the stated objective? Ignore access and cost constraints; those are enforced in code.",
                [
                    "No useful relevance",
                    "Indirect relevance",
                    "Directly relevant",
                    "Essential evidence",
                ],
            )
    elif task == "claim_detection":
        state = {"sentence": _text(parameters.get("sentence"), "sentence", 8000)}
        if "sentence_index" in parameters:
            index, span = parameters["sentence_index"], parameters.get("sentence_span")
            if (
                type(index) is not int
                or index < 0
                or not isinstance(span, (list, tuple))
                or len(span) != 2
                or any(type(value) is not int or value < 0 for value in span)
                or span[0] >= span[1]
            ):
                raise JevTaskError(
                    "invalid_task_input", "valid sentence index and span required"
                )
            state["sentence_index"] = index
            state["sentence_span"] = list(span)
            state["bounded_context"] = _text(
                parameters.get("bounded_context"), "claim context", 8000
            )
        questions["factual_claim"] = _noul(
            "Does `sentence` assert a falsifiable descriptive claim, as distinct from an opinion, instruction, question, or quoted claim that is only being reported?"
        )
    elif task == "checkworthiness":
        state = {
            "claim": _text(parameters.get("claim"), "claim", 8000),
            "topic": _text(parameters.get("topic"), "topic"),
        }
        questions["impact"] = _score(
            "How consequential would it be if this claim were wrong for the stated topic?",
            ["Trivial", "Limited", "Material", "High public impact"],
        )
        questions["testability"] = _score(
            "How feasible is it to check this claim against obtainable evidence?",
            ["Not checkable", "Difficult", "Checkable", "Directly checkable"],
        )
    elif task == "sentiment":
        state = {"target": _text(parameters.get("target"), "sentiment target")}
        if "passage_text" in parameters:
            state["passage_text"] = _text(
                parameters["passage_text"], "sentiment passage", 8000
            )
        questions["sentiment"] = _choice(
            "What sentiment does the captured passage express toward `target`? Distinguish a quoted speaker's attitude from the document author's attitude.",
            {
                "positive": "Favorable",
                "negative": "Unfavorable",
                "neutral": "No clear sentiment",
                "mixed": "Both favorable and unfavorable",
            },
        )
    elif task == "attribution":
        candidates = _options(
            parameters.get("candidates"),
            "attribution candidates",
            maximum=20,
            minimum=1,
        )
        state = {
            "statement": _text(parameters.get("statement"), "statement", 8000),
            "candidate_ids": list(candidates),
        }
        if "bounded_context" in parameters:
            state["bounded_context"] = _text(
                parameters["bounded_context"], "attribution context", 8000
            )
        questions["speaker"] = _choice(
            "Who among the supplied extracted candidates is directly attributed as the speaker of `statement`? Choose none or uncertain when appropriate; do not invent an actor.",
            {
                **candidates,
                "none": "No candidate is attributed",
                "uncertain": "Attribution remains ambiguous",
            },
        )
    return state, questions


def suggest_task(
    runtime: Any,
    namespace: str,
    run_id: str,
    task: str,
    parameters: Mapping[str, Any],
    source_refs: list[dict],
    *,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    max_attempts: int = 1,
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
    source_slices: list[dict[str, int]] | None = None,
) -> dict:
    """Execute one plan and return an explicitly non-authoritative suggestion."""
    state, questions = prepare_task(task, parameters, source_count=len(source_refs))
    run = runtime.run(
        namespace,
        run_id,
        f"jev-{task}-v1",
        state=state,
        questions=questions,
        source_refs=source_refs,
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=dict(policy),
        max_attempts=max_attempts,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
        **({"source_slices": source_slices} if source_slices is not None else {}),
    )
    shadow = run.get("rollout_mode") == "shadow"
    return {
        "contract": "noesis-jev-task-suggestion-v1",
        "task": task,
        "status": "shadow"
        if shadow
        else "suggested"
        if run.get("status") == "completed"
        else "unavailable",
        "accepted": False,
        "answers": {} if shadow else (run.get("receipt") or {}).get("answers", {}),
        "source_binding": run.get("source_binding", []),
        "decision_run": run,
    }
