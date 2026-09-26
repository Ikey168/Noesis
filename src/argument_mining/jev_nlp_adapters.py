"""Optional source-bound Jev suggestions for four local NLP tasks."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from src.argument_mining.jev_adapters import _sentence_spans
from src.kb.jev_evidence_adapters import _answered, _capture, _span
from src.kb.jev_tasks import suggest_task
from src.evaluation.jev_nlp import (
    validate_categorical_acceptance_policy,
    validate_claim_presence_policy,
)

SENTIMENT_LABELS = {"positive", "negative", "neutral", "mixed"}
ATTRIBUTION_SENTINELS = {"none", "uncertain"}
CHECKWORTHINESS_PRIORITY_POLICY = "checkworthiness-impact-60-testability-40-v1"


def _probability(value: Any) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _noul_answer(result: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    answer = _answered(result, key, "noul")
    if answer is None or not _probability(answer.get("value")):
        return None
    return answer


def _choice_answer(
    result: Mapping[str, Any], key: str, allowed: set[str]
) -> Mapping[str, Any] | None:
    answer = _answered(result, key, "choice")
    if answer is None:
        return None
    distribution = answer.get("probabilities")
    choice = answer.get("value")
    selected = answer.get("selected_probability")
    if (
        not isinstance(choice, str)
        or choice not in allowed
        or not isinstance(distribution, Mapping)
        or set(distribution) != allowed
        or any(not _probability(value) for value in distribution.values())
        or not math.isclose(sum(distribution.values()), 1.0, abs_tol=1e-6)
        or not _probability(selected)
        or not math.isclose(distribution[choice], selected, abs_tol=1e-9)
    ):
        return None
    return answer


def _policy_versions(result: Mapping[str, Any]) -> tuple[str, str]:
    decision_run = result.get("decision_run")
    if not isinstance(decision_run, Mapping):
        raise ValueError("durable decision run receipt required for calibrated suggestion")
    receipt = decision_run.get("receipt") or {}
    if not isinstance(receipt, Mapping):
        raise ValueError("durable decision receipt required for calibrated suggestion")
    model, rubric = receipt.get("model_requested"), receipt.get("rubric_id")
    if (
        not isinstance(model, str)
        or not model
        or not isinstance(rubric, str)
        or not rubric
    ):
        raise ValueError(
            "pinned model and rubric receipt required for calibrated suggestion"
        )
    return model, rubric


def _run(
    runtime: Any,
    namespace: str,
    run_id: str,
    task: str,
    parameters: dict,
    ref: dict,
    source_slice: dict[str, int],
    *,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    max_cost_usd_micros: int,
    deadline_s: float,
) -> dict:
    return suggest_task(
        runtime,
        namespace,
        run_id,
        task,
        parameters,
        [ref],
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=policy,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
        source_slices=[source_slice],
    )


def suggest_claim_presence(
    runtime: Any,
    namespace: str,
    run_id: str,
    source_ref: dict,
    *,
    sentence_index: int,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    calibration_policy: Mapping[str, Any] | None = None,
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
) -> dict[str, Any]:
    """Classify an existing sentence boundary; keep p(claim) semantics raw."""
    bindings, texts = _capture(runtime, namespace, principal_id, [source_ref], scopes)
    spans = _sentence_spans(texts[0])
    if type(sentence_index) is not int or not 0 <= sentence_index < len(spans):
        raise ValueError("sentence index is outside the captured source")
    sentence, start, end = spans[sentence_index]
    context_start = max(0, start - 1000)
    context_end = min(len(texts[0]), end + 1000)
    source_slice = {"start": context_start, "end": context_end}
    result = _run(
        runtime,
        namespace,
        run_id,
        "claim_detection",
        {
            "sentence": sentence,
            "sentence_index": sentence_index,
            "sentence_span": [start, end],
            "bounded_context": texts[0][context_start:context_end],
        },
        source_ref,
        source_slice,
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=policy,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
    )
    answer = _noul_answer(result, "factual_claim")
    p_claim = float(answer["value"]) if answer else None
    decision = None
    if calibration_policy is not None and p_claim is not None:
        model, rubric = _policy_versions(result)
        validate_claim_presence_policy(
            calibration_policy,
            model_version=model,
            rubric_version=rubric,
        )
        positive = calibration_policy["positive_threshold"]
        negative = calibration_policy["negative_threshold"]
        if not 0 <= negative < positive <= 1:
            raise ValueError("invalid claim decision thresholds")
        decision = (
            "claim"
            if p_claim >= positive
            else "nonclaim"
            if p_claim <= negative
            else None
        )
    selected_confidence = (
        p_claim
        if decision == "claim"
        else (1 - p_claim if decision == "nonclaim" else None)
    )
    return {
        "contract": "noesis-jev-claim-presence-suggestion-v1",
        "status": "suggested" if decision else "abstained",
        "accepted": False,
        "sentence_index": sentence_index,
        "text": sentence,
        "span": {"start": start, "end": end},
        "context_span": source_slice,
        "source_binding": result.get("source_binding") or bindings,
        "p_claim": p_claim,
        "p_nonclaim": 1 - p_claim if p_claim is not None else None,
        "selected_class_confidence": selected_confidence,
        "suggested_class": decision,
        "decision_run": result["decision_run"],
    }


def suggest_checkworthiness(
    runtime: Any,
    namespace: str,
    run_id: str,
    source_ref: dict,
    locator: Mapping[str, Any],
    *,
    claim_id: str,
    topic: str,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    priority_policy_id: str = CHECKWORTHINESS_PRIORITY_POLICY,
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
) -> dict[str, Any]:
    """Score a detected claim for optional priority without removing it."""
    if (
        not isinstance(claim_id, str)
        or not claim_id
        or priority_policy_id != CHECKWORTHINESS_PRIORITY_POLICY
    ):
        raise ValueError("detected claim and supported priority policy required")
    bindings, texts = _capture(runtime, namespace, principal_id, [source_ref], scopes)
    span, claim = _span(texts[0], locator)
    result = _run(
        runtime,
        namespace,
        run_id,
        "checkworthiness",
        {"claim": claim, "topic": topic},
        source_ref,
        span,
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=policy,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
    )
    impact = _answered(result, "impact", "score")
    testability = _answered(result, "testability", "score")

    def score(answer: Mapping[str, Any] | None) -> float | None:
        value = answer.get("value") if answer else None
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 3:
            return None
        return float(value)

    raw = {
        "impact": score(impact),
        "testability": score(testability),
    }
    priority = (
        (0.6 * raw["impact"] + 0.4 * raw["testability"]) / 3
        if all(
            value is not None
            for value in raw.values()
        )
        else None
    )
    return {
        "contract": "noesis-jev-checkworthiness-suggestion-v1",
        "status": "suggested" if priority is not None else "abstained",
        "accepted": False,
        "claim_id": claim_id,
        "claim_present": True,
        "truth_status": None,
        "source_binding": result.get("source_binding") or bindings,
        "locator": span,
        "raw_dimensions": raw,
        "priority_score": priority,
        "priority_policy_id": priority_policy_id,
        "scheduler_mutated": False,
        "decision_run": result["decision_run"],
    }


def suggest_sentiment(
    runtime: Any,
    namespace: str,
    run_id: str,
    source_ref: dict,
    locator: Mapping[str, Any],
    *,
    target: str,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    calibration_policy: Mapping[str, Any] | None = None,
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
) -> dict[str, Any]:
    """Map a contextual Choice to the existing label/score/text shape."""
    bindings, texts = _capture(runtime, namespace, principal_id, [source_ref], scopes)
    span, passage = _span(texts[0], locator)
    result = _run(
        runtime,
        namespace,
        run_id,
        "sentiment",
        {"target": target, "passage_text": passage},
        source_ref,
        span,
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=policy,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
    )
    answer = _choice_answer(result, "sentiment", SENTIMENT_LABELS)
    distribution = answer.get("probabilities") if answer else None
    choice = answer.get("value") if answer else None
    selected = None
    if calibration_policy is not None and distribution is not None:
        model, rubric = _policy_versions(result)
        validate_categorical_acceptance_policy(
            calibration_policy,
            task="sentiment",
            model_version=model,
            rubric_version=rubric,
        )
        threshold = calibration_policy.get("threshold")
        if _probability(threshold) and answer["selected_probability"] >= threshold:
            selected = choice
    return {
        "contract": "noesis-jev-sentiment-suggestion-v1",
        "status": "suggested" if selected else "abstained",
        "accepted": False,
        "target": target,
        "text": passage,
        "locator": span,
        "source_binding": result.get("source_binding") or bindings,
        "raw_distribution": dict(distribution) if distribution else None,
        "vendor_confidence": answer.get("vendor_confidence")
        if answer and _probability(answer.get("vendor_confidence"))
        else None,
        "suggested_sentiment": {
            "label": selected.upper(),
            "score": answer["selected_probability"],
            "text": passage,
        }
        if selected
        else None,
        "trend_mutated": False,
        "source_trust_inferred": False,
        "decision_run": result["decision_run"],
    }


def suggest_attribution(
    runtime: Any,
    namespace: str,
    run_id: str,
    source_ref: dict,
    statement_locator: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    calibration_policy: Mapping[str, Any] | None = None,
    authors: Sequence[str] = (),
    byline: str | None = None,
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
) -> dict[str, Any]:
    """Choose only among extracted speaker identities; preserve originals."""
    if (
        not isinstance(candidates, Sequence)
        or isinstance(candidates, (str, bytes))
        or not 1 <= len(candidates) <= 18
        or any(not isinstance(candidate, Mapping) for candidate in candidates)
    ):
        raise ValueError("bounded extracted candidates required")
    bindings, texts = _capture(runtime, namespace, principal_id, [source_ref], scopes)
    span, statement = _span(texts[0], statement_locator)
    choices = {}
    for candidate in candidates:
        identity, name = candidate.get("id"), candidate.get("name")
        role = candidate.get("role", "actor")
        if (
            not isinstance(identity, str)
            or not identity
            or identity in {"none", "uncertain"}
            or not isinstance(name, str)
            or not name
            or not isinstance(role, str)
            or not role
            or identity in choices
        ):
            raise ValueError("distinct extracted candidate IDs and names required")
        choices[identity] = f"{name} ({role})"
    if not isinstance(authors, Sequence) or isinstance(authors, (str, bytes)):
        raise ValueError("authors must be a sequence of original string identities")
    original_authors = list(authors)
    if any(not isinstance(author, str) for author in original_authors):
        raise ValueError("authors must preserve extracted string identities")
    if byline is not None and not isinstance(byline, str):
        raise ValueError("byline must be original text or null")
    context_start, context_end = (
        max(0, span["start"] - 500),
        min(len(texts[0]), span["end"] + 500),
    )
    context_span = {"start": context_start, "end": context_end}
    result = _run(
        runtime,
        namespace,
        run_id,
        "attribution",
        {
            "statement": statement,
            "candidates": choices,
            "bounded_context": texts[0][context_start:context_end],
        },
        source_ref,
        context_span,
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=policy,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
    )
    allowed_choices = set(choices) | ATTRIBUTION_SENTINELS
    answer = _choice_answer(result, "speaker", allowed_choices)
    choice = answer.get("value") if answer else None
    selected_choice = None
    if calibration_policy is not None and answer is not None:
        model, rubric = _policy_versions(result)
        validate_categorical_acceptance_policy(
            calibration_policy,
            task="attribution",
            model_version=model,
            rubric_version=rubric,
        )
        if (
            choice != "uncertain"
            and _probability(calibration_policy.get("threshold"))
            and answer["selected_probability"] >= calibration_policy["threshold"]
        ):
            selected_choice = choice

    return {
        "contract": "noesis-jev-attribution-suggestion-v1",
        "status": "suggested" if selected_choice is not None else "abstained",
        "accepted": False,
        "statement": statement,
        "statement_locator": span,
        "context_locator": context_span,
        "source_binding": result.get("source_binding") or bindings,
        "candidates": [dict(candidate) for candidate in candidates],
        "raw_choice": choice,
        "raw_distribution": dict(answer["probabilities"])
        if answer and answer.get("probabilities")
        else None,
        "suggested_choice": selected_choice,
        "selected_choice_confidence": answer.get("selected_probability")
        if answer and selected_choice is not None
        else None,
        "suggested_actor": next(
            (
                dict(candidate)
                for candidate in candidates
                if candidate["id"] == selected_choice
            ),
            None,
        ),
        "authors": original_authors,
        "byline": byline,
        "source_metadata_mutated": False,
        "decision_run": result["decision_run"],
    }
