"""Reproducible fuzzy-name benchmark for optional RapidFuzz candidate scoring."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import time
import tracemalloc
from difflib import SequenceMatcher
from typing import Any

from src.knowledge_graph.foundation.ontology import EntityType
from src.knowledge_graph.foundation.resolution import _normalize_name


def _sequence(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _rapid(a: str, b: str) -> float:
    try:
        from rapidfuzz.fuzz import ratio
    except ImportError as exc:  # pragma: no cover - depends on optional environment
        raise RuntimeError(
            "RapidFuzz is unavailable; install the evaluation extra"
        ) from exc
    return float(ratio(a, b)) / 100.0


def score_name_pair(
    entity_type: str | EntityType, left: str, right: str, *, backend: str
) -> float:
    """Score a pair after the same normalization used by the production resolver."""
    if isinstance(entity_type, EntityType):
        kind = entity_type
    else:
        value = str(entity_type).strip()
        try:
            kind = EntityType(value)
        except ValueError:
            kind = EntityType[value.upper()]
    a, b = _normalize_name(kind, left), _normalize_name(kind, right)
    if backend == "sequence-matcher":
        return _sequence(a, b)
    if backend == "rapidfuzz-ratio":
        return _rapid(a, b)
    raise ValueError("unknown fuzzy scoring backend")


def benchmark_fuzzy_resolution(
    cases: list[dict[str, Any]],
    thresholds: list[float] | None = None,
    *,
    test_cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare candidate scorers without changing production merge behavior.

    The benchmark treats fuzzy scores only as candidate-generation evidence.
    Exact identifiers, entity type checks, person-name ambiguity and review
    semantics remain production resolver responsibilities and are intentionally
    not bypassed here.
    """
    if not cases or len(cases) > 10_000:
        raise ValueError("one to 10000 bounded benchmark cases required")
    if test_cases is not None and (not test_cases or len(test_cases) > 10_000):
        raise ValueError("one to 10000 held-out cases required")
    for split in (cases, test_cases or []):
        ids = set()
        for case in split:
            if not isinstance(case, dict) or not {
                "id",
                "entity_type",
                "left",
                "right",
                "same_entity",
            } <= set(case):
                raise ValueError("benchmark case missing required fields")
            if type(case["same_entity"]) is not bool:
                raise ValueError("same_entity must be an explicit boolean label")
            if any(
                not isinstance(case[key], str) or not 1 <= len(case[key]) <= 1000
                for key in ("id", "entity_type", "left", "right")
            ):
                raise ValueError("bounded case identity/type/names required")
            if case["id"] in ids:
                raise ValueError("duplicate benchmark case identity")
            ids.add(case["id"])
            if test_cases is not None and (
                not isinstance(case.get("group_id"), str) or not case["group_id"]
            ):
                raise ValueError(
                    "explicit related-entity group_id required for frozen splits"
                )
    if test_cases is not None and (
        {case["group_id"] for case in cases} & {case["group_id"] for case in test_cases}
        or {case["id"] for case in cases} & {case["id"] for case in test_cases}
    ):
        raise ValueError("development/test split leakage")
    thresholds = thresholds or [0.70, 0.75, 0.80, 0.85, 0.88, 0.90, 0.95]
    if not thresholds or any(not 0 <= float(v) <= 1 for v in thresholds):
        raise ValueError("thresholds must lie between zero and one")

    backends = ["sequence-matcher", "rapidfuzz-ratio"]
    results: dict[str, Any] = {}
    for backend in backends:
        tracemalloc.start()
        started = time.perf_counter_ns()
        rows = []
        for case in cases:
            required = {"id", "entity_type", "left", "right", "same_entity"}
            if not required <= set(case):
                raise ValueError("benchmark case missing required fields")
            score = score_name_pair(
                case["entity_type"], case["left"], case["right"], backend=backend
            )
            rows.append(
                {
                    "id": str(case["id"]),
                    "score": score,
                    "same_entity": case["same_entity"],
                }
            )
        elapsed_ns = time.perf_counter_ns() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        curves = []
        positives = sum(row["same_entity"] for row in rows)
        negatives = len(rows) - positives
        for threshold in thresholds:
            predicted = [row for row in rows if row["score"] >= threshold]
            tp = sum(row["same_entity"] for row in predicted)
            fp = len(predicted) - tp
            curves.append(
                {
                    "threshold": float(threshold),
                    "candidate_recall": tp / positives if positives else 1.0,
                    "false_merge_rate": fp / negatives if negatives else 0.0,
                    "candidates": len(predicted),
                }
            )
        safe = [row for row in curves if row["false_merge_rate"] == 0]
        selected = max(
            safe,
            key=lambda row: (row["candidate_recall"], -row["threshold"]),
            default=None,
        )
        results[backend] = {
            "rows": rows,
            "threshold_curve": curves,
            "selected_zero_false_merge_threshold": selected,
            "latency_ns_total": elapsed_ns,
            "latency_ns_per_pair": elapsed_ns / len(rows),
            "peak_tracemalloc_bytes": peak,
        }
        if test_cases is not None:
            started = time.perf_counter_ns()
            heldout = [
                {
                    "id": case["id"],
                    "score": score_name_pair(
                        case["entity_type"],
                        case["left"],
                        case["right"],
                        backend=backend,
                    ),
                    "same_entity": case["same_entity"],
                }
                for case in test_cases
            ]
            elapsed = time.perf_counter_ns() - started
            threshold = selected["threshold"] if selected else None
            predicted = [
                row
                for row in heldout
                if threshold is not None and row["score"] >= threshold
            ]
            positives = sum(row["same_entity"] for row in heldout)
            negatives = len(heldout) - positives
            tp = sum(row["same_entity"] for row in predicted)
            results[backend]["heldout"] = {
                "rows": heldout,
                "threshold": threshold,
                "threshold_selected_on": "development",
                "candidate_recall": tp / positives if positives else None,
                "false_merge_rate": (len(predicted) - tp) / negatives
                if negatives
                else None,
                "candidates": len(predicted),
                "latency_ns_total": elapsed,
                "latency_ns_per_pair": elapsed / len(heldout),
            }

    rapid = results["rapidfuzz-ratio"]["selected_zero_false_merge_threshold"]
    baseline = results["sequence-matcher"]["selected_zero_false_merge_threshold"]
    if test_cases is None:
        decision = "defer"
        reason = "calibration-only run: a separate frozen held-out split is required for adoption"
    elif rapid is None:
        decision = "defer"
        reason = "no tested RapidFuzz threshold avoided hard-negative false merges"
    elif results["rapidfuzz-ratio"]["heldout"]["false_merge_rate"] != 0:
        decision = "defer"
        reason = "the development-calibrated threshold admits false matches on held-out names"
    elif (
        results["rapidfuzz-ratio"]["heldout"]["false_merge_rate"] == 0
        and results["rapidfuzz-ratio"]["heldout"]["candidate_recall"] is not None
        and (
            baseline is None
            or results["rapidfuzz-ratio"]["heldout"]["candidate_recall"]
            > (results["sequence-matcher"]["heldout"]["candidate_recall"] or 0)
        )
    ):
        decision = "adopt-for-optional-candidate-scoring"
        reason = "higher zero-false-merge candidate recall on the separate frozen held-out split"
    else:
        decision = "defer"
        reason = "no recall advantage over SequenceMatcher at zero false merges"
    return {
        "contract": "noesis-fuzzy-resolution-benchmark-v1",
        "case_count": len(cases),
        "test_case_count": len(test_cases or []),
        "evaluation_scope": "held-out"
        if test_cases is not None
        else "calibration-only",
        "split_sha256": {
            name: hashlib.sha256(
                json.dumps(values, sort_keys=True).encode()
            ).hexdigest()
            for name, values in [("development", cases), ("test", test_cases)]
        },
        "rapidfuzz_version": importlib.metadata.version("rapidfuzz"),
        "results": results,
        "decision": decision,
        "decision_reason": reason,
        "production_default_changed": False,
        "thresholds_reused_between_metrics": False,
        "false_merge_rate_semantics": "false positives among labelled negatives if fuzzy candidates were treated as matches; no actual merges executed",
        "identity_safeguards": [
            "exact identifiers retain precedence",
            "entity types remain isolated",
            "ambiguous people remain reviewable rather than fuzzy-auto-merged",
            "fuzzy scores are candidate evidence, not identity truth",
        ],
        "limitations": [
            "The bundled corpus is an authored regression benchmark, not independent human adjudication.",
            "tracemalloc measures Python allocations, not total process RSS.",
        ],
    }
