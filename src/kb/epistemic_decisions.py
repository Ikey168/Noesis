"""Optional hosted statement-kind classification at the epistemic callable seam."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from src.kb.epistemic import EpistemicError, EpistemicStore, STATUSES

TASK = "epistemic-statement-kind-v1"
RUBRIC = "epistemic-kind-taxonomy-v1"
MAX_STATEMENT_CHARS = 8_000
MAX_STATEMENT_ID_CHARS = 200

_CRITERIA = {
    "fact": "A directly asserted, falsifiable descriptive statement; this label does not verify truth",
    "report": "An assertion attributed to a speaker, document, or publication",
    "allegation": "A contested accusation or unverified assertion about conduct, including a quoted allegation",
    "estimate": "A present or past quantity expressed with measurement uncertainty",
    "forecast": "A prediction about a future state or event",
    "opinion": "A subjective evaluation, preference, or interpretation",
    "hypothesis": "A tentative explanatory proposition requiring investigation",
    "normative": "A claim about what ought, should, or must be done",
    "unknown": "The statement kind cannot be determined safely",
}


def epistemic_questions() -> dict[str, dict[str, Any]]:
    return {
        "kind": {
            "type": "choice",
            "instructions": "Classify how this statement is expressed, considering attribution, hedging, and quotations. Do not judge whether it is true. Choose unknown when the kind is ambiguous.",
            "criteria": dict(_CRITERIA),
        }
    }


def classifier_pin(policy: Mapping[str, Any]) -> dict[str, str]:
    model = policy.get("model")
    rubric = policy.get("rubric_id")
    if (
        not isinstance(model, str)
        or not model
        or not isinstance(rubric, str)
        or not rubric
    ):
        raise EpistemicError(
            "unpinned_classifier", "model and rubric revision are required"
        )
    return {"name": "typesafe-jev", "version": model, "revision": rubric}


def _probability(value: Any) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def classify_with_decision(
    runtime: Any,
    namespace: str,
    statement_id: str,
    statement: str,
    run_id: str,
    *,
    source_refs: Sequence[Mapping[str, Any]],
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    max_attempts: int = 1,
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 20.0,
    source_revision_id: str | None = None,
) -> dict[str, Any]:
    """Return a classifier result for ``classify_statement``.

    Runtime authorization precedes disclosure of the text. The returned
    probability means probability of the selected kind; vendor confidence is
    retained separately and never substituted for it.
    """
    if (
        not isinstance(statement_id, str)
        or not statement_id
        or len(statement_id) > MAX_STATEMENT_ID_CHARS
    ):
        raise EpistemicError("invalid_statement", "bounded statement identity is required")
    if not isinstance(statement, str):
        raise EpistemicError("invalid_statement", "statement must be text")
    text = statement.strip()
    if not text or len(text) > MAX_STATEMENT_CHARS:
        raise EpistemicError(
            "invalid_statement", "statement must contain 1–8000 characters"
        )
    if not source_refs:
        raise EpistemicError(
            "unbound_statement", "a versioned source reference is required"
        )
    if len(source_refs) > 20 or any(
        not isinstance(ref, Mapping) for ref in source_refs
    ):
        raise EpistemicError(
            "unbound_statement", "one to 20 versioned source references are required"
        )
    classifier_pin(policy)
    # Do not let an authorized reference serve as a decorative citation for
    # unrelated caller text. Resolve the references before building the
    # request, then send only the exact source span that contains the
    # statement. DecisionRuntime independently rechecks these exact versions
    # before and after hosted processing.
    _, captures = runtime.capture_sources(
        namespace, principal_id, [dict(ref) for ref in source_refs], scopes
    )
    if len(captures) != len(source_refs):
        raise EpistemicError(
            "unbound_statement", "authorized source text is unavailable"
        )
    match = None
    for index, capture in enumerate(captures):
        content = capture.get("content") if isinstance(capture, Mapping) else None
        if isinstance(content, str):
            start = content.find(text)
            if start >= 0:
                match = (index, start, start + len(text))
                break
    if match is None:
        raise EpistemicError(
            "unbound_statement",
            "statement must appear verbatim in an authorized source version",
        )
    source_index, start, end = match
    if (
        source_revision_id is not None
        and source_refs[source_index].get("revision_id") != source_revision_id
    ):
        raise EpistemicError(
            "unbound_statement",
            "assessment revision must match the classified source version",
        )
    bound_refs = [dict(source_refs[source_index])]
    run = runtime.run(
        namespace,
        run_id,
        TASK,
        state={
            "statement_id": statement_id,
            "statement": text,
            "statement_locator": {"start": start, "end": end},
        },
        questions=epistemic_questions(),
        source_refs=bound_refs,
        source_slices=[{"start": start, "end": end}],
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=dict(policy),
        max_attempts=max_attempts,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
    )
    receipt = run.get("receipt") or {}
    answer = receipt.get("answers", {}).get("kind") or {}
    if (
        run.get("status") != "completed"
        or run.get("rollout_mode") == "shadow"
        or answer.get("status") != "answered"
    ):
        return {
            "status": "unknown",
            "confidence": 0.0,
            "signals": [
                "shadow_mode"
                if run.get("rollout_mode") == "shadow"
                else "model_unavailable",
                "statement_kind_only",
            ],
            "decision_run": run,
            "truth_verified": False,
            "availability": "shadow"
            if run.get("rollout_mode") == "shadow"
            else "unavailable",
        }
    kind = answer.get("value")
    if not isinstance(kind, str) or kind not in STATUSES:
        raise EpistemicError(
            "invalid_classification", "classifier returned an unknown status"
        )
    probability = answer.get("selected_probability")
    distribution = answer.get("probabilities")
    vendor_confidence = answer.get("vendor_confidence")
    if (
        not _probability(probability)
        or not isinstance(distribution, Mapping)
        or set(distribution) != set(_CRITERIA)
        or any(not _probability(value) for value in distribution.values())
        or not math.isclose(sum(distribution.values()), 1.0, abs_tol=1e-6)
        or not math.isclose(distribution[kind], probability, abs_tol=1e-9)
        or vendor_confidence is not None and not _probability(vendor_confidence)
    ):
        raise EpistemicError(
            "invalid_classification",
            "valid selected-label probability, label distribution, and vendor confidence are required",
        )
    return {
        "status": kind,
        "confidence": probability,
        "selected_probability": probability,
        "vendor_confidence": vendor_confidence,
        "signals": ["statement_kind_only", "truth_not_verified"],
        "decision_run": run,
        "truth_verified": False,
        "availability": "answered",
    }


def assess_with_decision(
    store: EpistemicStore,
    runtime: Any,
    namespace: str,
    statement_id: str,
    text: str,
    evidence: Sequence[Mapping[str, Any]],
    run_id: str,
    *,
    source_refs: Sequence[Mapping[str, Any]],
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    source_revision_id: str | None = None,
) -> dict[str, Any]:
    """Persist a model kind while retaining the existing evidence assessment."""
    pin = classifier_pin(policy)

    def classify(statement: str) -> dict[str, Any]:
        return classify_with_decision(
            runtime,
            namespace,
            statement_id,
            statement,
            run_id,
            source_refs=source_refs,
            principal_id=principal_id,
            scopes=scopes,
            allow_remote=allow_remote,
            policy=policy,
            source_revision_id=source_revision_id,
        )

    return store.assess(
        namespace,
        statement_id,
        text,
        evidence,
        principal_id=principal_id,
        scopes=scopes,
        source_revision_id=source_revision_id,
        classifier=classify,
        classifier_pin=pin,
        policy={
            "assessment": "independence-weighted-v1",
            "statement_kind_rubric": policy["rubric_id"],
        },
    )
