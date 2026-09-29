"""Held-out evaluation of typed decision receipts against human judgments.

The evaluator never invokes a provider. Validation cases select an acceptance
threshold; test cases only report its behavior. Fixture labels are allowed for
exercising the plumbing, but cannot set a task-ready flag.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence

from src.evaluation.typed_decision_manifest import (
    coverage_report,
    validate_case_provenance,
    validate_manifest,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def _cases(
    rows: Sequence[Mapping], *, split: str, labels: tuple[str, ...]
) -> list[dict]:
    if not isinstance(rows, Sequence) or not 1 <= len(rows) <= 10000:
        raise ValueError("one to 10000 bounded evaluation cases required")
    seen = set()
    output = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("split") != split:
            raise ValueError("case split must match requested evaluation split")
        case_id, group = row.get("id"), row.get("group_id")
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in seen
            or not isinstance(group, str)
            or not group
        ):
            raise ValueError("unique case and related-document group IDs required")
        if row.get("label_origin") not in {
            "independent-human",
            "assisted-human",
            "fixture",
        }:
            raise ValueError("human or explicit fixture judgment origin required")
        if row.get("truth") not in labels:
            raise ValueError("truth must be an allowed task label")
        for key in ("source", "domain", "language", "content_type"):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError(f"case must identify {key}")
        status = row.get("status")
        if status not in {"completed", "unavailable", "failed", "abstained"}:
            raise ValueError("explicit result status required")
        probs = row.get("probabilities")
        if status == "completed":
            if not isinstance(probs, Mapping) or set(probs) != set(labels):
                raise ValueError("completed case requires the full label distribution")
            if (
                any(
                    type(p) not in {int, float}
                    or not math.isfinite(p)
                    or not 0 <= p <= 1
                    for p in probs.values()
                )
                or abs(sum(probs.values()) - 1) > 1e-4
            ):
                raise ValueError("invalid probability distribution")
        elif probs is not None:
            raise ValueError(
                "noncompleted cases may not carry successful probabilities"
            )
        latency = row.get("latency_ms")
        tokens = row.get("input_tokens")
        cost = row.get("total_cost_usd_micros")
        if latency is not None and (
            type(latency) not in {int, float}
            or not math.isfinite(latency)
            or latency < 0
        ):
            raise ValueError("invalid latency")
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("invalid token usage")
        if cost is not None and (type(cost) is not int or cost < 0):
            raise ValueError("invalid retry-inclusive cost")
        baseline = row.get("baseline_prediction")
        if baseline is not None and baseline not in labels:
            raise ValueError("baseline prediction must be an allowed label")
        if row.get("baseline_status") is not None and row["baseline_status"] not in {
            "completed",
            "unavailable",
            "failed",
            "abstained",
        }:
            raise ValueError("invalid baseline status")
        if baseline is not None and row.get("baseline_status") != "completed":
            raise ValueError("baseline prediction requires completed status")
        comparisons = row.get("baseline_predictions")
        if comparisons is not None:
            if (
                not isinstance(comparisons, Mapping)
                or not comparisons
                or set(comparisons) - {"local_default", "calibrated_full_window"}
            ):
                raise ValueError(
                    "only declared local and calibrated baseline comparisons allowed"
                )
            for value in comparisons.values():
                if (
                    not isinstance(value, Mapping)
                    or value.get("status")
                    not in {"completed", "unavailable", "failed", "abstained"}
                    or value.get("prediction") not in set(labels) | {None}
                    or value.get("status") == "completed"
                    and value.get("prediction") is None
                    or value.get("status") != "completed"
                    and value.get("prediction") is not None
                ):
                    raise ValueError(
                        "baseline needs explicit status and valid prediction"
                    )
        critical = row.get("critical_if_predicted", [])
        if not isinstance(critical, list) or any(
            label not in labels for label in critical
        ):
            raise ValueError("critical prediction labels must be task labels")
        seen.add(case_id)
        output.append(dict(row))
    return output


def fit_acceptance_policy(
    validation_rows: Sequence[Mapping],
    *,
    labels: Sequence[str],
    task: str,
    model_version: str,
    rubric_version: str,
    minimum_coverage: float = 0.8,
    release_criteria: Mapping[str, float] | None = None,
    coverage_requirements: Mapping[str, object] | None = None,
) -> dict:
    """Select a probability floor using only the validation split."""
    classes = tuple(labels)
    if not 2 <= len(classes) <= 255 or len(set(classes)) != len(classes):
        raise ValueError("two to 255 unique task labels required")
    if not all(isinstance(x, str) and x for x in (task, model_version, rubric_version)):
        raise ValueError("task, model and rubric identities required")
    if type(minimum_coverage) not in {int, float} or not 0 <= minimum_coverage <= 1:
        raise ValueError("minimum coverage must be in [0,1]")
    allowed_criteria = {
        "min_macro_f1",
        "min_accepted_coverage",
        "min_delta_macro_f1",
        "max_critical_error_rate",
        "max_p95_latency_ms",
        "max_retry_inclusive_cost_usd_micros",
    }
    if release_criteria is not None:
        if (
            not isinstance(release_criteria, Mapping)
            or not release_criteria
            or set(release_criteria) - allowed_criteria
        ):
            raise ValueError("release criteria must use supported predeclared metrics")
        if any(
            type(value) not in {int, float} or not math.isfinite(value)
            for value in release_criteria.values()
        ):
            raise ValueError("release criteria must be finite numbers")
        for key in ("min_macro_f1", "min_accepted_coverage", "max_critical_error_rate"):
            if key in release_criteria and not 0 <= release_criteria[key] <= 1:
                raise ValueError("probability release criteria must be in [0,1]")
        for key in ("max_p95_latency_ms", "max_retry_inclusive_cost_usd_micros"):
            if key in release_criteria and release_criteria[key] < 0:
                raise ValueError(
                    "cost and latency release criteria must be nonnegative"
                )
    rows = _cases(validation_rows, split="validation", labels=classes)
    manifest = (
        validate_manifest(coverage_requirements)
        if coverage_requirements is not None
        else None
    )
    if manifest is not None:
        for row in rows:
            validate_case_provenance(
                row, model_version=model_version, rubric_version=rubric_version
            )
    best = None
    for threshold in (0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        accepted = [
            r
            for r in rows
            if r["status"] == "completed"
            and max(r["probabilities"].values()) >= threshold
        ]
        coverage = len(accepted) / len(rows)
        accuracy = (
            sum(
                max(r["probabilities"], key=r["probabilities"].get) == r["truth"]
                for r in accepted
            )
            / len(accepted)
            if accepted
            else 0.0
        )
        candidate = (coverage >= minimum_coverage, accuracy, threshold)
        if best is None or candidate > best:
            best = candidate
    policy = {
        "contract": "noesis-typed-decision-policy-v1",
        "task": task,
        "model_version": model_version,
        "rubric_version": rubric_version,
        "labels": list(classes),
        "threshold": best[2],
        "minimum_coverage": minimum_coverage,
        "validation_ids": sorted(r["id"] for r in rows),
        "validation_groups": sorted({r["group_id"] for r in rows}),
        "validation_sha256": _digest(rows),
        "label_origins": sorted({r["label_origin"] for r in rows}),
        "release_criteria": dict(release_criteria)
        if release_criteria is not None
        else None,
        "coverage_requirements": manifest,
        "validation_coverage": coverage_report(rows, classes, manifest)
        if manifest is not None
        else None,
        "task_ready": False,
    }
    policy["policy_sha256"] = _digest(policy)
    return policy


def evaluate_acceptance_policy(
    test_rows: Sequence[Mapping],
    policy: Mapping,
    *,
    input_usd_per_million: float = 0.042,
) -> dict:
    """Score a frozen test split; unavailable and abstained cases stay in the denominator."""
    core = {k: v for k, v in policy.items() if k != "policy_sha256"}
    if policy.get("contract") != "noesis-typed-decision-policy-v1" or _digest(
        core
    ) != policy.get("policy_sha256"):
        raise ValueError("invalid or changed policy")
    labels = tuple(policy["labels"])
    rows = _cases(test_rows, split="test", labels=labels)
    manifest = policy.get("coverage_requirements")
    if manifest is not None:
        validate_manifest(manifest)
        for row in rows:
            validate_case_provenance(
                row,
                model_version=policy["model_version"],
                rubric_version=policy["rubric_version"],
            )
    test_coverage = (
        coverage_report(rows, labels, manifest) if manifest is not None else None
    )
    ids = {r["id"] for r in rows}
    groups = {r["group_id"] for r in rows}
    if ids & set(policy["validation_ids"]) or groups & set(policy["validation_groups"]):
        raise ValueError("validation and held-out test cases overlap")
    if (
        type(input_usd_per_million) not in {int, float}
        or not math.isfinite(input_usd_per_million)
        or input_usd_per_million < 0
    ):
        raise ValueError("invalid token price")
    counts = Counter()
    per_class = {label: Counter() for label in labels}
    brier = []
    calibration_bins = [[] for _ in range(10)]
    latencies = []
    total_tokens = 0
    total_cost = 0
    cost_count = 0
    baseline_rows = 0
    baseline_per_class = {label: Counter() for label in labels}
    named_baselines: dict[str, dict[str, Counter]] = {}
    strata: dict[str, dict[str, Counter]] = {"content_type": {}, "language": {}}
    errors = []
    critical_errors = 0
    for row in rows:
        counts[row["status"]] += 1
        if row.get("latency_ms") is not None:
            latencies.append(float(row["latency_ms"]))
        total_tokens += row.get("input_tokens") or 0
        if row.get("total_cost_usd_micros") is not None:
            total_cost += row["total_cost_usd_micros"]
            cost_count += 1
        probs = row.get("probabilities")
        choice = max(probs, key=probs.get) if probs else None
        accepted = choice is not None and probs[choice] >= policy["threshold"]
        predicted = choice if accepted else None
        counts["accepted"] += accepted
        counts["correct"] += predicted == row["truth"] if accepted else 0
        for dimension in strata:
            bucket = strata[dimension].setdefault(row[dimension], Counter())
            bucket["total"] += 1
            bucket["accepted"] += accepted
            bucket["correct"] += predicted == row["truth"] if accepted else 0
        if row.get("baseline_status") is not None:
            baseline_rows += 1
            baseline_prediction = (
                row.get("baseline_prediction")
                if row["baseline_status"] == "completed"
                else None
            )
            for label in labels:
                baseline_per_class[label]["tp"] += (
                    baseline_prediction == label and row["truth"] == label
                )
                baseline_per_class[label]["fp"] += (
                    baseline_prediction == label and row["truth"] != label
                )
                baseline_per_class[label]["fn"] += (
                    baseline_prediction != label and row["truth"] == label
                )
        for name, baseline_result in row.get("baseline_predictions", {}).items():
            per_label = named_baselines.setdefault(
                name, {label: Counter() for label in labels}
            )
            guess = (
                baseline_result["prediction"]
                if baseline_result["status"] == "completed"
                else None
            )
            for label in labels:
                per_label[label]["tp"] += guess == label and row["truth"] == label
                per_label[label]["fp"] += guess == label and row["truth"] != label
                per_label[label]["fn"] += guess != label and row["truth"] == label
        if probs:
            brier.append(
                sum((probs[label] - (row["truth"] == label)) ** 2 for label in labels)
            )
            calibration_bins[min(9, int(probs[choice] * 10))].append(
                (probs[choice], choice == row["truth"])
            )
        for label in labels:
            per_class[label]["tp"] += predicted == label and row["truth"] == label
            per_class[label]["fp"] += predicted == label and row["truth"] != label
            per_class[label]["fn"] += predicted != label and row["truth"] == label
        if predicted != row["truth"]:
            errors.append(
                {
                    "id": row["id"],
                    "expected": row["truth"],
                    "predicted": predicted,
                    "status": row["status"],
                }
            )
        if predicted != row["truth"] and predicted in row.get(
            "critical_if_predicted", []
        ):
            critical_errors += 1
    n = len(rows)
    if baseline_rows not in {0, n}:
        raise ValueError("baseline must cover every held-out case for comparison")
    names = {name for row in rows for name in row.get("baseline_predictions", {})}
    if names and any(set(row.get("baseline_predictions", {})) != names for row in rows):
        raise ValueError(
            "each named baseline must cover the complete held-out test set"
        )
    metrics = {}
    for label, c in per_class.items():
        precision = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0.0
        recall = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0.0
        metrics[label] = {
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0,
            "support": c["tp"] + c["fn"],
        }
    ece = (
        sum(
            len(bucket)
            / sum(map(len, calibration_bins))
            * abs(
                statistics.mean(x[0] for x in bucket)
                - statistics.mean(x[1] for x in bucket)
            )
            for bucket in calibration_bins
            if bucket
        )
        if any(calibration_bins)
        else None
    )
    baseline_macro_f1 = None
    if baseline_rows:
        baseline_scores = []
        for c in baseline_per_class.values():
            precision = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0.0
            recall = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0.0
            baseline_scores.append(
                2 * precision * recall / (precision + recall)
                if precision + recall
                else 0.0
            )
        baseline_macro_f1 = statistics.mean(baseline_scores)
    macro_f1 = statistics.mean(v["f1"] for v in metrics.values())
    named_baseline_metrics = {}
    for name, per_label in named_baselines.items():
        scores = []
        for value in per_label.values():
            precision = (
                value["tp"] / (value["tp"] + value["fp"])
                if value["tp"] + value["fp"]
                else 0.0
            )
            recall = (
                value["tp"] / (value["tp"] + value["fn"])
                if value["tp"] + value["fn"]
                else 0.0
            )
            scores.append(
                2 * precision * recall / (precision + recall)
                if precision + recall
                else 0.0
            )
        named_baseline_metrics[name] = {"macro_f1": statistics.mean(scores)}
    measurements = {
        "min_macro_f1": macro_f1,
        "min_accepted_coverage": counts["accepted"] / n,
        "min_delta_macro_f1": macro_f1 - baseline_macro_f1
        if baseline_macro_f1 is not None
        else None,
        "max_critical_error_rate": critical_errors / n,
        "max_p95_latency_ms": _percentile(latencies, 0.95)
        if len(latencies) == n
        else None,
        "max_retry_inclusive_cost_usd_micros": total_cost if cost_count == n else None,
    }
    criteria = policy.get("release_criteria")
    gates = {
        key: {
            "target": target,
            "measured": measurements[key],
            "passed": measurements[key] is not None
            and (
                measurements[key] >= target
                if key.startswith("min_")
                else measurements[key] <= target
            ),
        }
        for key, target in (criteria or {}).items()
    }
    return {
        "contract": "noesis-typed-decision-evaluation-v1",
        "task": policy["task"],
        "policy_sha256": policy["policy_sha256"],
        "test_sha256": _digest(rows),
        "n": n,
        "label_origins": sorted({r["label_origin"] for r in rows}),
        "independent_human_test": all(
            r["label_origin"] == "independent-human" for r in rows
        ),
        "accepted_coverage": counts["accepted"] / n,
        "accuracy_all_cases": counts["correct"] / n,
        "accuracy_accepted": counts["correct"] / counts["accepted"]
        if counts["accepted"]
        else None,
        "macro_f1": macro_f1,
        "baseline_macro_f1": baseline_macro_f1,
        "named_baselines": named_baseline_metrics,
        "delta_macro_f1": macro_f1 - baseline_macro_f1
        if baseline_macro_f1 is not None
        else None,
        "strata": {
            dimension: {
                value: {
                    "n": bucket["total"],
                    "accepted_coverage": bucket["accepted"] / bucket["total"],
                    "accuracy_all_cases": bucket["correct"] / bucket["total"],
                }
                for value, bucket in sorted(groups.items())
            }
            for dimension, groups in strata.items()
        },
        "per_class": metrics,
        "brier_completed": statistics.mean(brier) if brier else None,
        "ece_completed": ece,
        "statuses": {
            key: counts[key]
            for key in ("completed", "abstained", "unavailable", "failed")
        },
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "input_tokens": total_tokens,
        "estimated_input_usd": total_tokens / 1_000_000 * input_usd_per_million,
        "total_retry_inclusive_cost_usd_micros": total_cost
        if cost_count == n
        else None,
        "cost_coverage": cost_count / n,
        "critical_error_rate": critical_errors / n,
        "release_gates": gates,
        "release_criteria_passed": bool(gates)
        and all(gate["passed"] for gate in gates.values())
        and policy["label_origins"] == ["independent-human"]
        and all(r["label_origin"] == "independent-human" for r in rows)
        and (
            test_coverage is None
            or (policy["validation_coverage"]["complete"] and test_coverage["complete"])
        ),
        "benchmark_coverage": {
            "validation": policy.get("validation_coverage"),
            "test": test_coverage,
        },
        "errors": errors,
        "task_ready": False,
        "limitation": "Held-out metrics and human labels require independent verification before adoption.",
    }
