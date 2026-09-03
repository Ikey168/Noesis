"""Offline deterministic calibration and evaluation for origin inference."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.osint.independence import (
    DEFAULT_NEAR_DUPLICATE_THRESHOLD,
    compare_signals,
    extract_document_signals,
)


class _UnionFind:
    def __init__(self, items: Sequence[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def _interval(successes: int, total: int, z: float = 1.96) -> dict[str, Any]:
    if total == 0:
        return {"value": None, "lo": None, "hi": None, "level": 0.95, "n": 0}
    value = successes / total
    denominator = 1 + z * z / total
    centre = (value + z * z / (2 * total)) / denominator
    half = z * math.sqrt(value * (1 - value) / total + z * z / (4 * total * total)) / denominator
    return {
        "value": round(value, 4),
        "lo": round(max(0.0, centre - half), 4),
        "hi": round(min(1.0, centre + half), 4),
        "level": 0.95,
        "n": total,
    }


def _predict(case: Mapping[str, Any], threshold: float) -> dict[str, Any]:
    documents = list(case["documents"])
    ids = [str(row["document_id"]) for row in documents]
    signals = {str(row["document_id"]): extract_document_signals(row) for row in documents}
    uf = _UnionFind(ids)
    pair_decisions = {}
    for left_index, left in enumerate(ids):
        for right in ids[left_index + 1 :]:
            decision = compare_signals(
                signals[left], signals[right], near_duplicate_threshold=threshold
            )
            pair_decisions[(left, right)] = decision
            if decision["dependent"]:
                uf.union(left, right)
    sizes: dict[str, int] = {}
    for document_id in ids:
        root = uf.find(document_id)
        sizes[root] = sizes.get(root, 0) + 1
    states = {
        document_id: (
            "known_independent"
            if signals[document_id].get("original_reporting")
            else "likely_dependent"
            if sizes[uf.find(document_id)] > 1
            else "unknown"
        )
        for document_id in ids
    }
    return {"roots": {item: uf.find(item) for item in ids}, "states": states, "pairs": pair_decisions}


def evaluate_cases(cases: Sequence[Mapping[str, Any]], threshold: float) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    exact_clusters = 0
    unknown_correct = 0
    unknown_expected = 0
    unknown_predicted = 0
    documents_total = 0
    false_independence: list[dict[str, str]] = []
    for case in cases:
        predicted = _predict(case, threshold)
        documents = list(case["documents"])
        expected = {str(row["document_id"]): row.get("expected_origin") for row in documents}
        expected_states = {
            str(row["document_id"]): row.get("expected_state", "unknown") for row in documents
        }
        ids = sorted(expected)
        case_exact = True
        for left_index, left in enumerate(ids):
            for right in ids[left_index + 1 :]:
                expected_same = bool(expected[left] and expected[left] == expected[right])
                predicted_same = predicted["roots"][left] == predicted["roots"][right]
                if expected_same and predicted_same:
                    tp += 1
                elif expected_same and not predicted_same:
                    fn += 1
                    case_exact = False
                    false_independence.append(
                        {"case": str(case["name"]), "left": left, "right": right}
                    )
                elif not expected_same and predicted_same:
                    fp += 1
                    case_exact = False
                else:
                    tn += 1
        exact_clusters += int(case_exact)
        documents_total += len(ids)
        for document_id in ids:
            expected_unknown = expected_states[document_id] == "unknown"
            predicted_unknown = predicted["states"][document_id] == "unknown"
            unknown_expected += int(expected_unknown)
            unknown_predicted += int(predicted_unknown)
            unknown_correct += int(expected_unknown and predicted_unknown)
    precision_total = tp + fp
    recall_total = tp + fn
    return {
        "threshold": threshold,
        "sample_sizes": {
            "cases": len(cases),
            "documents": documents_total,
            "pairs": tp + fp + fn + tn,
            "expected_dependent_pairs": recall_total,
        },
        "pairwise": {
            "true_positive": tp,
            "false_positive": fp,
            "false_independence": fn,
            "true_negative": tn,
            "precision": _interval(tp, precision_total),
            "recall": _interval(tp, recall_total),
        },
        "cluster_exact_match": _interval(exact_clusters, len(cases)),
        "unknown": {
            "expected": unknown_expected,
            "predicted": unknown_predicted,
            "correct": unknown_correct,
            "coverage": _interval(unknown_correct, unknown_expected),
            "predicted_rate": _interval(unknown_predicted, documents_total),
        },
        "false_independence_cases": false_independence,
    }


def calibrate_threshold(
    development_cases: Sequence[Mapping[str, Any]],
    candidates: Sequence[float] = (0.6, 0.66, 0.72, 0.78, 0.84),
) -> dict[str, Any]:
    """Select only on development fixtures; false independence costs 3x."""
    trials = []
    for threshold in candidates:
        report = evaluate_cases(development_cases, float(threshold))
        pairwise = report["pairwise"]
        cost = 3 * pairwise["false_independence"] + pairwise["false_positive"]
        trials.append({"threshold": float(threshold), "cost": cost, "metrics": report})
    selected = min(
        trials,
        key=lambda row: (
            row["cost"],
            row["metrics"]["pairwise"]["false_positive"],
            abs(row["threshold"] - DEFAULT_NEAR_DUPLICATE_THRESHOLD),
        ),
    )
    return {
        "selected_threshold": selected["threshold"],
        "objective": "3 * false_independence + false_dependency",
        "trials": trials,
        "partition": "development",
    }


def evaluate_fixture_files(development_path: Path, final_path: Path) -> dict[str, Any]:
    development = json.loads(development_path.read_text())
    final = json.loads(final_path.read_text())
    calibration = calibrate_threshold(development["cases"])
    threshold = calibration["selected_threshold"]
    return {
        "method": "offline deterministic pairwise and cluster evaluation",
        "method_version": "origin-evaluation-v1",
        "calibration": calibration,
        "development": evaluate_cases(development["cases"], threshold),
        "final_test": evaluate_cases(final["cases"], threshold),
        "partitions": {
            "threshold_selected_from": "development",
            "final_test_used_for_selection": False,
        },
        "error_costs": {
            "false_independence": 3,
            "false_dependency": 1,
            "rationale": "overstating independent corroboration is the higher-cost error",
        },
    }


__all__ = ["calibrate_threshold", "evaluate_cases", "evaluate_fixture_files"]
