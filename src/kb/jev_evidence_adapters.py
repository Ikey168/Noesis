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
    long_unchecked = max(len(text_a), len(text_b)) > 4000 and (
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
    relation = None
    if all(label in RELATIONS for label in labels):
        if set(labels) == {"entailment", "contradiction"}:
            relation = None  # conflicting directions need review
        elif (
            labels == ["entailment", "entailment"] and similarity >= duplicate_threshold
        ):
            relation = "duplicate"
        elif labels == ["contradiction", "contradiction"]:
            relation = "contradicts"
        elif labels[0] == "entailment":
            relation = "a_supports_b"
        elif labels[1] == "entailment":
            relation = "b_supports_a"
        elif labels[0] == "contradiction":
            relation = "a_contradicts_b"
        elif labels[1] == "contradiction":
            relation = "b_contradicts_a"
        else:
            relation = "neutral"
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
