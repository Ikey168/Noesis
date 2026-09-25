"""Offline, held-out comparison hooks for Jev evidence suggestions."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def _p95(values: list[float]) -> float | None:
    return sorted(values)[math.ceil(0.95 * len(values)) - 1] if values else None


def _ranking_metrics(
    order: list[str], relevant: Mapping[str, int], answer_bearing: set[str], k: int
) -> dict:
    selected = order[:k]
    gains = [
        (2 ** relevant[item] - 1) / math.log2(index + 2)
        for index, item in enumerate(selected)
    ]
    ideal = sorted(relevant.values(), reverse=True)[:k]
    ideal_gain = sum(
        (2**value - 1) / math.log2(index + 2) for index, value in enumerate(ideal)
    )
    return {
        "ndcg": sum(gains) / ideal_gain if ideal_gain else 0.0,
        "answer_bearing_recall": len(set(selected) & answer_bearing)
        / len(answer_bearing)
        if answer_bearing
        else 1.0,
    }


def evaluate_jev_reranking(
    cases: Sequence[Mapping[str, Any]], *, k: int = 5, allow_fixture: bool = False
) -> dict:
    """Compare fusion, MiniLM, Qwen and Jev on identical held-out candidates."""
    if not 1 <= len(cases) <= 1000 or type(k) is not int or not 1 <= k <= 30:
        raise ValueError("bounded held-out queries and k required")
    methods = ("fusion", "minilm", "qwen", "jev")
    aggregate = {method: [] for method in methods}
    latency = []
    cost = []
    accepted = 0
    origins = set()
    query_ids = set()
    for case in cases:
        if case.get("split") != "test" or case.get("label_origin") not in {
            "independent-human",
            "fixture",
        }:
            raise ValueError("explicit held-out human or fixture judgments required")
        if case["label_origin"] == "fixture" and not allow_fixture:
            raise ValueError("fixture labels require explicit allow_fixture=True")
        query_id = case.get("query_id")
        if not isinstance(query_id, str) or not query_id or query_id in query_ids:
            raise ValueError("unique held-out query identities required")
        query_ids.add(query_id)
        origins.add(case["label_origin"])
        relevance = case.get("relevance")
        rankings = case.get("rankings")
        if (
            not isinstance(relevance, Mapping)
            or not 1 <= len(relevance) <= 30
            or not isinstance(rankings, Mapping)
            or set(rankings) != set(methods)
            or any(
                type(value) is not int or not 0 <= value <= 3
                for value in relevance.values()
            )
        ):
            raise ValueError("same bounded candidates and graded relevance required")
        ids = set(relevance)
        answer_bearing = set(case.get("answer_bearing", []))
        if not answer_bearing <= ids:
            raise ValueError("answer-bearing identities must be candidates")
        for method in methods:
            order = rankings[method]
            if (
                not isinstance(order, list)
                or set(order) != ids
                or len(order) != len(ids)
            ):
                raise ValueError("every backend must rank the identical shortlist")
            aggregate[method].append(
                _ranking_metrics(order, relevance, answer_bearing, k)
            )
        if case.get("jev_status") == "suggested":
            accepted += 1
        elif case.get("jev_status") != "fallback_original_order":
            raise ValueError("explicit Jev suggestion or fallback status required")
        if case.get("latency_ms") is not None:
            value = case["latency_ms"]
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise ValueError("nonnegative finite latency required")
            latency.append(float(value))
        if case.get("cost_usd_micros") is not None:
            value = case["cost_usd_micros"]
            if type(value) is not int or value < 0:
                raise ValueError("nonnegative cost required")
            cost.append(value)
    return {
        "contract": "noesis-jev-rerank-evaluation-v1",
        "n": len(cases),
        "k": k,
        "methods": {
            method: {
                key: sum(row[key] for row in aggregate[method]) / len(cases)
                for key in ("ndcg", "answer_bearing_recall")
            }
            for method in methods
        },
        "accepted_coverage": accepted / len(cases),
        "p95_latency_ms": _p95(latency) if len(latency) == len(cases) else None,
        "total_retry_inclusive_cost_usd_micros": sum(cost)
        if len(cost) == len(cases)
        else None,
        "label_origins": sorted(origins),
        "task_ready": False,
    }


def evaluate_jev_claim_relations(
    cases: Sequence[Mapping[str, Any]], *, allow_fixture: bool = False
) -> dict:
    """Report false edge rates for Jev and local NLI on the same human pairs."""
    if not 1 <= len(cases) <= 10000:
        raise ValueError("bounded relation test cases required")
    classes = {
        "duplicate",
        "a_supports_b",
        "b_supports_a",
        "contradicts",
        "a_contradicts_b",
        "b_contradicts_a",
    }
    methods = ("jev", "local_nli")
    counts = {
        method: {
            kind: {"predicted": 0, "false": 0}
            for kind in ("duplicate", "supports", "contradicts")
        }
        for method in methods
    }
    origins = set()
    pair_ids = set()
    for case in cases:
        if case.get("split") != "test" or case.get("label_origin") not in {
            "independent-human",
            "fixture",
        }:
            raise ValueError("held-out label provenance required")
        if case["label_origin"] == "fixture" and not allow_fixture:
            raise ValueError("fixture labels require explicit allow_fixture=True")
        pair_id = case.get("pair_id")
        if not isinstance(pair_id, str) or not pair_id or pair_id in pair_ids:
            raise ValueError("unique held-out claim pair identities required")
        pair_ids.add(pair_id)
        origins.add(case["label_origin"])
        truth = case.get("truth")
        if truth not in classes | {"neutral"}:
            raise ValueError("gold relation outside ontology")
        for method in methods:
            value = case.get(method)
            if value not in classes | {"neutral", "abstained"}:
                raise ValueError("explicit relation or abstention required")
            kind = (
                "supports"
                if value in {"a_supports_b", "b_supports_a"}
                else "contradicts"
                if value in {"contradicts", "a_contradicts_b", "b_contradicts_a"}
                else value
            )
            if kind in counts[method]:
                counts[method][kind]["predicted"] += 1
                if kind == "supports":
                    correct = value == truth
                elif kind == "contradicts":
                    correct = truth in {
                        "contradicts",
                        "a_contradicts_b",
                        "b_contradicts_a",
                    }
                else:
                    correct = value == truth
                counts[method][kind]["false"] += not correct
    return {
        "contract": "noesis-jev-claim-relation-evaluation-v1",
        "n": len(cases),
        "methods": {
            method: {
                kind: {
                    **value,
                    "false_rate": value["false"] / value["predicted"]
                    if value["predicted"]
                    else None,
                }
                for kind, value in counts[method].items()
            }
            for method in methods
        },
        "label_origins": sorted(origins),
        "task_ready": False,
    }


def evaluate_jev_answer_support(
    cases: Sequence[Mapping[str, Any]], *, allow_fixture: bool = False
) -> dict:
    """Compare exact-citation support judgments against held-out labels.

    The evaluator keeps abstentions in the overall denominator and reports
    false entailment and contradiction rates separately. Fixture labels test
    the measurement path but cannot make the task ready for rollout.
    """
    if not 1 <= len(cases) <= 10000:
        raise ValueError("bounded answer-support test cases required")
    classes = {"entailment", "contradiction", "neutral", "unavailable"}
    methods = ("jev", "local_nli")
    required_kinds = {
        "unsupported_citation",
        "contradiction",
        "correction",
        "incomplete_coverage",
        "appropriate_refusal",
    }
    counts = {
        method: {
            "predicted": {kind: 0 for kind in classes},
            "false": {kind: 0 for kind in classes},
            "correct": 0,
            "abstained": 0,
            "relevance_correct": 0,
            "relevance_cases": 0,
            "relevance_unavailable": 0,
            "refusal_correct": 0,
            "false_refusals": 0,
            "missed_refusals": 0,
        }
        for method in methods
    }
    origins = set()
    ids = set()
    kinds = set()
    citation_states = {"current", "missing", "stale"}
    coverage_incomplete = 0
    missing_citations = 0
    stale_citations = 0
    for case in cases:
        if case.get("split") != "test" or case.get("label_origin") not in {
            "independent-human",
            "fixture",
        }:
            raise ValueError("held-out human or fixture labels required")
        if case["label_origin"] == "fixture" and not allow_fixture:
            raise ValueError("fixture labels require explicit allow_fixture=True")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError("unique answer-support case identities required")
        ids.add(case_id)
        kind = case.get("case_kind")
        if kind not in required_kinds:
            raise ValueError("required answer-support case kind missing or invalid")
        kinds.add(kind)
        citation_state = case.get("citation_state")
        if citation_state not in citation_states:
            raise ValueError("current, missing or stale citation state required")
        coverage_complete = case.get("coverage_complete")
        if type(coverage_complete) is not bool:
            raise ValueError("explicit answer evidence coverage required")
        coverage_incomplete += not coverage_complete
        missing_citations += citation_state == "missing"
        stale_citations += citation_state == "stale"
        source_revision = case.get("source_revision")
        locator = case.get("locator")
        if citation_state == "missing":
            if source_revision is not None or locator is not None:
                raise ValueError(
                    "missing citation cases cannot invent a source revision or locator"
                )
        elif not isinstance(source_revision, str) or not source_revision or not isinstance(locator, Mapping):
            raise ValueError("pinned source revision and citation locator required")
        origins.add(case["label_origin"])
        truth = case.get("truth")
        if truth not in classes:
            raise ValueError("gold citation relation outside ontology")
        if citation_state in {"missing", "stale"} and truth != "unavailable":
            raise ValueError("missing or stale citations must be unavailable in gold labels")
        if kind == "incomplete_coverage" and coverage_complete:
            raise ValueError("incomplete-coverage cases must record incomplete coverage")
        should_refuse = case.get("should_refuse")
        relevant = case.get("relevant")
        if type(should_refuse) is not bool or type(relevant) is not bool:
            raise ValueError("human relevance and refusal labels required")
        for method in methods:
            value = case.get(method)
            if value not in classes | {"abstained"}:
                raise ValueError("explicit relation or abstention required")
            prediction_refused = case.get(f"{method}_refused")
            prediction_relevant = case.get(f"{method}_relevant")
            if type(prediction_refused) is not bool:
                raise ValueError("explicit predicted refusal required for every method")
            if prediction_relevant is not None and type(prediction_relevant) is not bool:
                raise ValueError("relevance prediction must be boolean or unavailable")
            counts[method]["refusal_correct"] += prediction_refused == should_refuse
            counts[method]["false_refusals"] += prediction_refused and not should_refuse
            counts[method]["missed_refusals"] += not prediction_refused and should_refuse
            if prediction_relevant is None:
                counts[method]["relevance_unavailable"] += 1
            else:
                counts[method]["relevance_cases"] += 1
                counts[method]["relevance_correct"] += prediction_relevant == relevant
            if value == "abstained":
                counts[method]["abstained"] += 1
                continue
            counts[method]["predicted"][value] += 1
            counts[method]["false"][value] += value != truth
            counts[method]["correct"] += value == truth
    reports = {}
    for method in methods:
        current = counts[method]
        reports[method] = {
            "accuracy_over_all_cases": current["correct"] / len(cases),
            "coverage": sum(current["predicted"].values()) / len(cases),
            "abstentions": current["abstained"],
            "relevance_accuracy": current["relevance_correct"] / len(cases),
            "relevance_coverage": current["relevance_cases"] / len(cases),
            "relevance_unavailable": current["relevance_unavailable"],
            "refusal_accuracy": current["refusal_correct"] / len(cases),
            "false_refusals": current["false_refusals"],
            "missed_refusals": current["missed_refusals"],
            "per_relation": {
                relation: {
                    "predicted": current["predicted"][relation],
                    "false": current["false"][relation],
                    "false_rate": (
                        current["false"][relation] / current["predicted"][relation]
                        if current["predicted"][relation]
                        else None
                    ),
                }
                for relation in sorted(classes)
            },
        }
    if kinds != required_kinds:
        missing = sorted(required_kinds - kinds)
        raise ValueError(f"answer-support evaluation must cover all required case kinds: {missing}")
    return {
        "contract": "noesis-jev-answer-support-evaluation-v1",
        "n": len(cases),
        "case_kinds": sorted(kinds),
        "coverage_incomplete_cases": coverage_incomplete,
        "missing_citations": missing_citations,
        "stale_citations": stale_citations,
        "methods": reports,
        "label_origins": sorted(origins),
        "task_ready": False,
        "readiness_requires": "held-out independent human citation judgments and measured false-support/contradiction limits",
    }
