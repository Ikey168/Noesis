"""Held-out stance/frame evaluation for optional Jev mining suggestions."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from src.argument_mining.model_diagnostics import prf
from src.evaluation.mining_runtime import apply_policy, fit_policy, validate_policy


def fit_jev_mining_policy(
    validation_rows: Sequence[Mapping[str, Any]],
    *,
    task: str,
    labels: Sequence[str],
    model_version: str,
    rubric_version: str,
    minimum_coverage: float = 0.8,
) -> dict:
    """Fit one task policy on completed validation receipts only.

    Human labels are supplied externally; fixture labels exercise plumbing but
    do not make the task ready for production selection.
    """
    if not validation_rows or any(
        row.get("split") != "validation" for row in validation_rows
    ):
        raise ValueError("validation-only rows required")
    completed = [row for row in validation_rows if row.get("status") == "completed"]
    if not completed:
        raise ValueError("completed validation receipts required")
    return fit_policy(
        completed,
        labels,
        task=task,
        model_version=model_version,
        template_version=rubric_version,
        minimum_coverage=minimum_coverage,
    )


def evaluate_jev_mining(
    test_rows: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> dict:
    """Evaluate the frozen test split, retaining abstentions in denominators.

    Each row has `labels`, `scores`, `status`, and optionally
    `baseline_predictions` keyed by `legacy_snippet` or
    `calibrated_full_window`. Frame rows carry `dominant_truth`.
    """
    validate_policy(policy)
    task = policy["task"]
    labels = list(policy["labels"])
    if task not in {"stance", "frames"} or not test_rows:
        raise ValueError("supported task and nonempty test split required")
    validation_ids = set(policy["validation_ids"])
    validation_groups = set(policy["validation_groups"])
    ids = set()
    truth: list[set[str]] = []
    predicted: list[set[str]] = []
    rows = []
    for row in test_rows:
        if row.get("split") != "test" or row.get("status") not in {
            "completed",
            "abstained",
            "unavailable",
            "failed",
        }:
            raise ValueError("explicit held-out test status required")
        row_id, group_id = row.get("id"), row.get("group_id")
        if (
            not isinstance(row_id, str)
            or not row_id
            or row_id in ids
            or row_id in validation_ids
            or group_id in validation_groups
            or not isinstance(group_id, str)
            or not group_id
        ):
            raise ValueError("unique unrelated held-out cases required")
        if row.get("label_origin") not in {
            "independent-human",
            "assisted-human",
            "fixture",
        }:
            raise ValueError("explicit label origin required")
        if not isinstance(row.get("source_type"), str) or not row["source_type"]:
            raise ValueError("source content type required")
        gold = set(row.get("labels", []))
        if not gold <= set(labels) or (task == "stance" and len(gold) != 1):
            raise ValueError("invalid task labels")
        if task == "frames" and row.get("dominant_truth") not in (gold or {"other"}):
            raise ValueError("dominant truth must be one marked frame or other")
        if row["status"] == "completed":
            scores = row.get("scores")
            if (
                not isinstance(scores, list)
                or len(scores) != len(labels)
                or any(
                    type(value) not in {int, float}
                    or not math.isfinite(value)
                    or not 0 <= value <= 1
                    for value in scores
                )
            ):
                raise ValueError("completed receipt needs all finite class scores")
            outcome = apply_policy(scores, policy)
            choice = set(outcome["labels"])
        else:
            if row.get("scores") is not None:
                raise ValueError("noncompleted receipt cannot carry successful scores")
            choice = set()
        baseline = row.get("baseline_predictions", {})
        if not isinstance(baseline, Mapping) or set(baseline) - {
            "legacy_snippet",
            "calibrated_full_window",
        }:
            raise ValueError("unsupported baseline mapping")
        for value in baseline.values():
            if not isinstance(value, list) or not set(value) <= set(labels):
                raise ValueError("baseline predictions must use task labels")
        rows.append(dict(row))
        ids.add(row_id)
        truth.append(gold)
        predicted.append(choice)
    metric = prf(truth, predicted, labels)
    by_content_type = {}
    for source_type in sorted({row["source_type"] for row in rows}):
        indices = [
            index for index, row in enumerate(rows) if row["source_type"] == source_type
        ]
        by_content_type[source_type] = prf(
            [truth[index] for index in indices],
            [predicted[index] for index in indices],
            labels,
        )
    baselines = {}
    for baseline in ("legacy_snippet", "calibrated_full_window"):
        if all(baseline in row.get("baseline_predictions", {}) for row in rows):
            guesses = [set(row["baseline_predictions"][baseline]) for row in rows]
            baselines[baseline] = prf(truth, guesses, labels)
    dominant_accuracy = None
    if task == "frames":
        dominant = [
            max(
                (label for label in labels if label in predicted[index]),
                key=lambda label: row["scores"][labels.index(label)],
            )
            if predicted[index]
            else "other"
            for index, row in enumerate(rows)
        ]
        dominant_accuracy = sum(
            actual == row["dominant_truth"]
            for actual, row in zip(dominant, rows, strict=True)
        ) / len(rows)
    statuses = {
        status: sum(row["status"] == status for row in rows)
        for status in ("completed", "abstained", "unavailable", "failed")
    }
    return {
        "contract": "noesis-jev-mining-evaluation-v1",
        "task": task,
        "policy_sha256": policy["policy_sha256"],
        "n": len(rows),
        "macro_f1": metric["macro_f1"],
        "per_label": metric["per_class"],
        "subset_accuracy": metric["exact_accuracy"],
        "dominant_accuracy": dominant_accuracy,
        "accepted_coverage": sum(bool(value) for value in predicted) / len(rows),
        "by_content_type": by_content_type,
        "baselines": baselines,
        "statuses": statuses,
        "label_origins": sorted({row["label_origin"] for row in rows}),
        "task_ready": False,
        "readiness_requires": "held-out independent human labels, measured gains and domain review",
    }
