"""Optional source-bound Jev suggestions for retrieved and cited evidence.

No adapter writes accepted answers, claim links, or OSINT edges. Existing local
selection, citation, independence and temporal rules remain authoritative.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from src.kb.jev_tasks import suggest_task

RELATIONS = {"entailment", "contradiction", "neutral"}


def _capture(
    runtime: Any, namespace: str, principal_id: str, refs: list[dict], scopes: set[str]
) -> tuple[list[dict], list[str]]:
    bindings, captures = runtime.capture_sources(namespace, principal_id, refs, scopes)
    if len(bindings) != len(refs) or len(captures) != len(refs):
        raise ValueError("every exact source version must be captured")
    texts = [capture.get("content") for capture in captures]
    if any(not isinstance(value, str) or not value for value in texts):
        raise ValueError("captured source text is unavailable")
    return bindings, texts


def _span(
    text: str, locator: Mapping[str, Any], *, maximum: int = 8000
) -> tuple[dict[str, int], str]:
    start, end = locator.get("start"), locator.get("end")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(text)
        or end - start > maximum
    ):
        raise ValueError("bounded exact source character span required")
    passage = text[start:end]
    quote = locator.get("quote")
    if quote is not None and quote != passage:
        raise ValueError("citation quote differs from current source revision")
    return {"start": start, "end": end}, passage


def _answered(
    suggestion: Mapping[str, Any], key: str, kind: str
) -> Mapping[str, Any] | None:
    if suggestion.get("status") != "suggested":
        return None
    answer = suggestion.get("answers", {}).get(key)
    if (
        not isinstance(answer, Mapping)
        or answer.get("kind") != kind
        or answer.get("status") != "answered"
    ):
        return None
    return answer


def suggest_shortlist_rerank(
    runtime: Any,
    namespace: str,
    run_id: str,
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
) -> dict[str, Any]:
    """Score an existing bounded shortlist; return original order on failure.

    Each candidate carries `source_ref`, `locator`, `original_score` and
    optional citation/source metadata. The caller's context assembler still
    applies source diversity, independence, token and citation policy.
    """
    if not isinstance(candidates, Sequence) or not 1 <= len(candidates) <= 20:
        raise ValueError("one to 20 retrieved candidates required")
    refs = [dict(candidate["source_ref"]) for candidate in candidates]
    bindings, texts = _capture(runtime, namespace, principal_id, refs, scopes)
    slices = []
    for candidate, text in zip(candidates, texts, strict=True):
        span, passage = _span(text, candidate["locator"])
        if candidate.get("content") is not None and candidate["content"] != passage:
            raise ValueError("retrieved candidate differs from exact source span")
        score = candidate.get("original_score")
        if type(score) not in {int, float} or not math.isfinite(score):
            raise ValueError("finite original retrieval score required")
        slices.append(span)
    result = suggest_task(
        runtime,
        namespace,
        run_id,
        "reranking",
        {
            "query": query,
            "candidate_trace": [
                {
                    "original_index": index,
                    "original_score": float(candidate["original_score"]),
                }
                for index, candidate in enumerate(candidates)
            ],
        },
        refs,
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=policy,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
        source_slices=slices,
    )
    rows = []
    all_answered = True
    for index, candidate in enumerate(candidates):
        relevance = _answered(result, f"relevant_{index}", "noul")
        bearing = _answered(result, f"answer_bearing_{index}", "noul")
        valid = all(
            answer is not None
            and type(answer.get("value")) in {int, float}
            and math.isfinite(answer["value"])
            and 0 <= answer["value"] <= 1
            for answer in (relevance, bearing)
        )
        all_answered &= valid
        row = {
            "original_index": index,
            "original_score": float(candidate["original_score"]),
            "source_ref": refs[index],
            "source_binding": (result.get("source_binding") or bindings)[index],
            "locator": slices[index],
            "source": candidate.get("source"),
            "origin": candidate.get("origin"),
            "citation": candidate.get("citation"),
            "content": candidate.get(
                "content", texts[index][slices[index]["start"] : slices[index]["end"]]
            ),
            "title": candidate.get("title", ""),
            "url": candidate.get("url", ""),
            "relevance_score": float(relevance["value"]) if valid else None,
            "answer_bearing_score": float(bearing["value"]) if valid else None,
            "suggested_score": (
                0.7 * float(relevance["value"]) + 0.3 * float(bearing["value"])
            )
            if valid
            else None,
        }
        row["rerank_score"] = row["suggested_score"] if valid else row["original_score"]
        row["final_score"] = row["rerank_score"]
        rows.append(row)
    # Never remove a source or suppress the shortlist on a partial failure.
    ranked = (
        sorted(rows, key=lambda row: (-row["suggested_score"], row["original_index"]))
        if all_answered
        else rows
    )
    return {
        "contract": "noesis-jev-rerank-suggestion-v1",
        "status": "suggested" if all_answered else "fallback_original_order",
        "accepted": False,
        "candidate_count": len(rows),
        "ranking": ranked,
        "decision_run": result["decision_run"],
        "selection_policy_applied": False,
    }


def suggest_answer_support(
    runtime: Any,
    namespace: str,
    run_id: str,
    statement: str,
    citation: Mapping[str, Any],
    *,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
) -> dict[str, Any]:
    """Assess one statement against an exact cited passage, without promotion."""
    ref = dict(citation["source_ref"])
    bindings, texts = _capture(runtime, namespace, principal_id, [ref], scopes)
    locator, passage = _span(texts[0], citation["locator"])
    result = suggest_task(
        runtime,
        namespace,
        run_id,
        "answer_support",
        {"statement": statement, "locator": locator, "passage_text": passage},
        [ref],
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=policy,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
        source_slices=[locator],
    )
    answer = _answered(result, "relation", "choice")
    relation = answer.get("value") if answer else None
    if relation not in RELATIONS | {"unavailable"}:
        relation = None
    return {
        "contract": "noesis-jev-answer-support-suggestion-v1",
        "status": "suggested" if relation in RELATIONS else "abstained",
        "accepted": False,
        "relation": relation,
        "probabilities": dict(answer["probabilities"])
        if answer and answer.get("probabilities")
        else None,
        "vendor_confidence": answer.get("vendor_confidence") if answer else None,
        "source_ref": ref,
        "source_binding": result.get("source_binding") or bindings,
        "locator": locator,
        "citation": dict(citation),
        "counts_as_independent_source": False,
        "decision_run": result["decision_run"],
    }


#: Claim spans longer than this need a complete local-window coverage result
#: before a hosted relation is asked.
LONG_CLAIM_CHARS = 4000


def derive_claim_relation(
    labels: Sequence[Any], similarity: float, duplicate_threshold: float
) -> str | None:
    """Pair relation from the two directional Choice labels (None = abstain).

    Shared by per-link and batched review so both apply identical rules.
    """
    if len(labels) != 2 or not all(label in RELATIONS for label in labels):
        return None
    labels = list(labels)
    if set(labels) == {"entailment", "contradiction"}:
        return None  # conflicting directions need review
    if labels == ["entailment", "entailment"] and similarity >= duplicate_threshold:
        return "duplicate"
    if labels == ["contradiction", "contradiction"]:
        return "contradicts"
    if labels[0] == "entailment":
        return "a_supports_b"
    if labels[1] == "entailment":
        return "b_supports_a"
    if labels[0] == "contradiction":
        return "a_contradicts_b"
    if labels[1] == "contradiction":
        return "b_contradicts_a"
    return "neutral"


def suggest_claim_relation(
    runtime: Any,
    namespace: str,
    run_id: str,
    claim_a: Mapping[str, Any],
    claim_b: Mapping[str, Any],
    *,
    similarity: float,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    duplicate_threshold: float = 0.88,
    window_relations: Sequence[str] | None = None,
    window_coverage_complete: bool = False,
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
) -> dict[str, Any]:
    """Assess both claim directions, preserving duplicate and temporal gates."""
    if (
        type(similarity) not in {int, float}
        or not math.isfinite(similarity)
        or not 0 <= similarity <= 1
        or type(duplicate_threshold) not in {int, float}
        or not math.isfinite(duplicate_threshold)
        or not 0 <= duplicate_threshold <= 1
    ):
        raise ValueError("finite similarity and duplicate threshold required")
    if type(window_coverage_complete) is not bool:
        raise ValueError("explicit boolean window coverage result required")
    refs = [dict(claim_a["source_ref"]), dict(claim_b["source_ref"])]
    bindings, texts = _capture(runtime, namespace, principal_id, refs, scopes)
    locator_a, text_a = _span(texts[0], claim_a["locator"])
    locator_b, text_b = _span(texts[1], claim_b["locator"])
    if refs[0] == refs[1] and locator_a == locator_b:
        return {
            "contract": "noesis-jev-claim-relation-suggestion-v1",
            "status": "abstained",
            "reason": "same_claim_span",
            "accepted": False,
            "relation": None,
            "a_to_b": None,
            "b_to_a": None,
            "similarity": float(similarity),
            "duplicate_threshold": duplicate_threshold,
            "source_binding": bindings,
            "locators": [locator_a, locator_b],
            "graph_write": False,
            "temporal_transition": None,
            "decision_run": None,
        }
    if window_relations is not None and (
        not isinstance(window_relations, Sequence)
        or isinstance(window_relations, (str, bytes))
        or not 1 <= len(window_relations) <= 64
        or any(label not in RELATIONS for label in window_relations)
    ):
        raise ValueError("bounded local window relation labels required")
    window_conflict = window_relations is not None and {
        "entailment",
        "contradiction",
    } <= set(window_relations)
    long_unchecked = max(len(text_a), len(text_b)) > LONG_CLAIM_CHARS and (
        window_relations is None or window_coverage_complete is not True
    )
    if window_conflict or long_unchecked:
        return {
            "contract": "noesis-jev-claim-relation-suggestion-v1",
            "status": "abstained",
            "reason": "conflicting_windows"
            if window_conflict
            else "window_coverage_unverified",
            "accepted": False,
            "relation": None,
            "a_to_b": None,
            "b_to_a": None,
            "similarity": float(similarity),
            "duplicate_threshold": duplicate_threshold,
            "window_coverage_complete": window_coverage_complete is True,
            "source_binding": bindings,
            "locators": [locator_a, locator_b],
            "graph_write": False,
            "temporal_transition": None,
            "decision_run": None,
        }
    result = suggest_task(
        runtime,
        namespace,
        run_id,
        "claim_links",
        {"claim_a": text_a, "claim_b": text_b},
        refs,
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=policy,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
        source_slices=[locator_a, locator_b],
    )
    forward = _answered(result, "a_to_b", "choice")
    backward = _answered(result, "b_to_a", "choice")
    labels = [answer.get("value") if answer else None for answer in (forward, backward)]
    relation = derive_claim_relation(labels, similarity, duplicate_threshold)
    return {
        "contract": "noesis-jev-claim-relation-suggestion-v1",
        "status": "suggested" if relation else "abstained",
        "accepted": False,
        "relation": relation,
        "a_to_b": labels[0],
        "b_to_a": labels[1],
        "similarity": float(similarity),
        "duplicate_threshold": duplicate_threshold,
        "window_coverage_complete": window_coverage_complete is True,
        "source_binding": result.get("source_binding") or bindings,
        "locators": [locator_a, locator_b],
        "graph_write": False,
        "temporal_transition": None,
        "decision_run": result["decision_run"],
    }


CONTRADICTION_CONFLICT_KINDS = frozenset({"direct", "quantitative"})
CONTRADICTION_RECONCILABLE_KINDS = frozenset({"temporal", "scope", "attribution"})


def _probability(answer: Mapping[str, Any] | None) -> float | None:
    if answer is None:
        return None
    value = answer.get("value")
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return float(value)


def _threshold(value: Any, name: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value < 1:
        raise ValueError(f"{name} must be a finite probability in (0, 1)")
    return float(value)


def _claim_pair(
    runtime: Any,
    namespace: str,
    principal_id: str,
    scopes: set[str],
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> tuple[list[dict], list[dict], list[dict[str, int]], tuple[str, str]]:
    refs = [dict(first["source_ref"]), dict(second["source_ref"])]
    bindings, texts = _capture(runtime, namespace, principal_id, refs, scopes)
    locator_a, text_a = _span(texts[0], first["locator"])
    locator_b, text_b = _span(texts[1], second["locator"])
    return refs, bindings, [locator_a, locator_b], (text_a, text_b)


def derive_contradiction_assessment(
    kind: str | None,
    jointly: float | None,
    same: float | None,
    jointly_true_threshold: float = 0.5,
    same_subject_threshold: float = 0.5,
) -> tuple[str | None, str | None]:
    """``(assessment, reason)`` from the Choice kind and two Noul answers.

    Shared by per-link and batched review so both apply identical rules.
    """
    if kind is None or jointly is None or same is None:
        return None, "unanswered"
    if kind == "unclear":
        return None, "unclear"
    if kind in CONTRADICTION_CONFLICT_KINDS:
        if jointly < jointly_true_threshold and same >= same_subject_threshold:
            return "conflict", None
        return None, "inconsistent_answers"
    if kind in CONTRADICTION_RECONCILABLE_KINDS:
        return "reconcilable", None
    if jointly >= jointly_true_threshold:  # not_contradiction
        return "no_conflict", None
    return None, "inconsistent_answers"


def suggest_contradiction_kind(
    runtime: Any,
    namespace: str,
    run_id: str,
    claim_a: Mapping[str, Any],
    claim_b: Mapping[str, Any],
    *,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    local_relation: str | None = None,
    jointly_true_threshold: float = 0.5,
    same_subject_threshold: float = 0.5,
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
) -> dict[str, Any]:
    """Type an already flagged contradiction as a secondary Jev assessment.

    Local NLI and the conflict graph flag many pairs that are not genuine
    contradictions: the claims describe different times, places or
    populations, or one merely reports an allegation. This asks one Choice
    (the kind of disagreement) and two independent Noul questions (can both
    hold; same subject) over the exact claim spans, then derives an
    ``assessment`` in code:

    * ``conflict`` — a direct or quantitative kind that both Noul answers
      agree with (cannot jointly hold, same subject);
    * ``reconcilable`` — temporal, scope or attribution differences;
    * ``no_conflict`` — compatible claims that can jointly hold;
    * abstention — unclear, unanswered, or internally inconsistent answers.

    ``local_relation`` (e.g. the stored ``contradicts`` edge or conflict type)
    is echoed for comparison but never sent to the provider, so the hosted
    judgment is not anchored on the local label. Nothing is written to
    ``claim_links`` or ``claim_conflicts``; thresholds are uncalibrated
    defaults until a human-labeled evaluation fixes them.
    """
    jointly_true_threshold = _threshold(jointly_true_threshold, "jointly_true_threshold")
    same_subject_threshold = _threshold(same_subject_threshold, "same_subject_threshold")
    if local_relation is not None and (
        not isinstance(local_relation, str) or not 1 <= len(local_relation) <= 64
    ):
        raise ValueError("bounded local relation label required")
    refs, bindings, locators, (text_a, text_b) = _claim_pair(
        runtime, namespace, principal_id, scopes, claim_a, claim_b
    )
    base = {
        "contract": "noesis-jev-contradiction-kind-suggestion-v1",
        "accepted": False,
        "local_relation": local_relation,
        "locators": locators,
        "graph_write": False,
        "thresholds": {
            "jointly_true": jointly_true_threshold,
            "same_subject": same_subject_threshold,
            "calibrated": False,
        },
    }
    if refs[0] == refs[1] and locators[0] == locators[1]:
        return {
            **base,
            "status": "abstained",
            "reason": "same_claim_span",
            "kind": None,
            "assessment": None,
            "jointly_true_probability": None,
            "same_subject_probability": None,
            "source_binding": bindings,
            "decision_run": None,
        }
    result = suggest_task(
        runtime,
        namespace,
        run_id,
        "contradiction_kind",
        {"claim_a": text_a, "claim_b": text_b},
        refs,
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=policy,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
        source_slices=locators,
    )
    from src.kb.jev_tasks import CONTRADICTION_KINDS

    kind_answer = _answered(result, "kind", "choice")
    kind = kind_answer.get("value") if kind_answer else None
    if kind not in CONTRADICTION_KINDS:
        kind = None
    jointly = _probability(_answered(result, "jointly_true", "noul"))
    same = _probability(_answered(result, "same_subject", "noul"))
    assessment, reason = derive_contradiction_assessment(
        kind, jointly, same, jointly_true_threshold, same_subject_threshold
    )
    return {
        **base,
        "status": "suggested" if assessment else "abstained",
        "reason": reason,
        "kind": kind,
        "assessment": assessment,
        "agrees_with_local": None
        if assessment is None or local_relation is None
        else assessment == "conflict",
        "probabilities": dict(kind_answer["probabilities"])
        if kind_answer and kind_answer.get("probabilities")
        else None,
        "vendor_confidence": kind_answer.get("vendor_confidence") if kind_answer else None,
        "jointly_true_probability": jointly,
        "same_subject_probability": same,
        "source_binding": result.get("source_binding") or bindings,
        "decision_run": result["decision_run"],
    }


def suggest_corroboration(
    runtime: Any,
    namespace: str,
    run_id: str,
    claim: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
) -> dict[str, Any]:
    """Assess whether an exact evidence span corroborates an exact claim span.

    A secondary check for the corroboration ledger
    (:func:`src.osint.corroboration.corroborate`): it can confirm or dispute
    that a stored ``supports``/``contradicts`` edge is borne out by the text.
    It never adds a source, never counts as an independent origin (origin
    lineage stays authoritative for independence), and never writes
    ``claim_evidence`` or ``claim_links``. ``independent_report_probability``
    is a reading of the text only, reported for reviewers.
    """
    refs, bindings, locators, (claim_text, evidence_text) = _claim_pair(
        runtime, namespace, principal_id, scopes, claim, evidence
    )
    base = {
        "contract": "noesis-jev-corroboration-suggestion-v1",
        "accepted": False,
        "locators": locators,
        "graph_write": False,
        "counts_as_independent_source": False,
    }
    if refs[0] == refs[1] and locators[0] == locators[1]:
        return {
            **base,
            "status": "abstained",
            "reason": "same_claim_span",
            "relation": None,
            "independent_report_probability": None,
            "source_binding": bindings,
            "decision_run": None,
        }
    result = suggest_task(
        runtime,
        namespace,
        run_id,
        "corroboration",
        {"claim": claim_text, "evidence": evidence_text},
        refs,
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=policy,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
        source_slices=locators,
    )
    from src.kb.jev_tasks import CORROBORATION_RELATIONS

    answer = _answered(result, "relation", "choice")
    relation = answer.get("value") if answer else None
    if relation not in CORROBORATION_RELATIONS or relation == "unclear":
        reason = "unclear" if relation == "unclear" else "unanswered"
        relation = None
    else:
        reason = None
    return {
        **base,
        "status": "suggested" if relation else "abstained",
        "reason": reason,
        "relation": relation,
        "probabilities": dict(answer["probabilities"])
        if answer and answer.get("probabilities")
        else None,
        "vendor_confidence": answer.get("vendor_confidence") if answer else None,
        "independent_report_probability": _probability(
            _answered(result, "independent_report", "noul")
        ),
        "source_binding": result.get("source_binding") or bindings,
        "decision_run": result["decision_run"],
    }
