"""Frozen offline evaluation hooks for optional Jev NLP suggestions."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from src.argument_mining.model_diagnostics import prf


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _rows(rows: Sequence[Mapping[str, Any]], split: str) -> list[dict]:
    if not 1 <= len(rows) <= 10000:
        raise ValueError("bounded evaluation rows required")
    seen = set()
    output = []
    for row in rows:
        identity = row.get("id")
        if (
            row.get("split") != split
            or not isinstance(identity, str)
            or not identity
            or identity in seen
            or not isinstance(row.get("group_id"), str)
            or not row["group_id"]
            or row.get("label_origin")
            not in {"independent-human", "assisted-human", "fixture"}
        ):
            raise ValueError("unique split, group and label provenance required")
        for key in ("source_type", "language", "case_kind"):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError(f"explicit {key} required")
        output.append(dict(row))
        seen.add(identity)
    return output


def fit_claim_presence_policy(
    validation_rows: Sequence[Mapping[str, Any]],
    *,
    model_version: str,
    rubric_version: str,
    minimum_coverage: float = 0.8,
) -> dict:
    """Fit separate positive/negative p(claim) thresholds on validation only."""
    rows = _rows(validation_rows, "validation")
    if not model_version or not rubric_version or not 0 <= minimum_coverage <= 1:
        raise ValueError("pinned model/rubric and coverage target required")
    for row in rows:
        if (
            row.get("truth") not in {"claim", "nonclaim"}
            or type(row.get("p_claim")) not in {int, float}
            or not math.isfinite(row["p_claim"])
            or not 0 <= row["p_claim"] <= 1
        ):
            raise ValueError("validation needs binary gold and finite p_claim")
    best = None
    for negative in (0.1, 0.2, 0.3, 0.4, 0.5):
        for positive in (0.5, 0.6, 0.7, 0.8, 0.9):
            if negative >= positive:
                continue
            guesses = [
                {"claim"}
                if row["p_claim"] >= positive
                else {"nonclaim"}
                if row["p_claim"] <= negative
                else set()
                for row in rows
            ]
            coverage = sum(bool(value) for value in guesses) / len(rows)
            metric = prf(
                [{row["truth"]} for row in rows], guesses, ["claim", "nonclaim"]
            )
            candidate = (
                coverage >= minimum_coverage,
                metric["macro_f1"],
                coverage,
                negative,
                positive,
            )
            if best is None or candidate > best:
                best = candidate
    policy = {
        "contract": "noesis-jev-binary-policy-v1",
        "task": "claim_detection",
        "model_version": model_version,
        "rubric_version": rubric_version,
        "negative_threshold": best[3],
        "positive_threshold": best[4],
        "validation_ids": sorted(row["id"] for row in rows),
        "validation_groups": sorted({row["group_id"] for row in rows}),
        "validation_sha256": _hash(rows),
        "label_origins": sorted({row["label_origin"] for row in rows}),
        "task_ready": False,
    }
    policy["policy_sha256"] = _hash(policy)
    return policy


def validate_claim_presence_policy(
    policy: Mapping[str, Any],
    *,
    model_version: str | None = None,
    rubric_version: str | None = None,
) -> None:
    negative = policy.get("negative_threshold")
    positive = policy.get("positive_threshold")
    if (
        policy.get("contract") != "noesis-jev-binary-policy-v1"
        or policy.get("task") != "claim_detection"
        or not isinstance(policy.get("model_version"), str)
        or not policy.get("model_version")
        or not isinstance(policy.get("rubric_version"), str)
        or not policy.get("rubric_version")
        or _hash(
            {key: value for key, value in policy.items() if key != "policy_sha256"}
        )
        != policy.get("policy_sha256")
        or model_version is not None
        and policy.get("model_version") != model_version
        or rubric_version is not None
        and policy.get("rubric_version") != rubric_version
        or not _finite_probability(negative)
        or not _finite_probability(positive)
        or negative >= positive
    ):
        raise ValueError("changed or mismatched claim calibration policy")


def fit_categorical_acceptance_policy(
    validation_rows: Sequence[Mapping[str, Any]],
    *,
    task: str,
    model_version: str,
    rubric_version: str,
    minimum_coverage: float = 0.8,
) -> dict:
    """Freeze one selected-probability gate from validation judgments only."""
    if (
        task not in {"sentiment", "attribution"}
        or not model_version
        or not rubric_version
        or not 0 <= minimum_coverage <= 1
    ):
        raise ValueError("supported pinned categorical task required")
    rows = _rows(validation_rows, "validation")
    for row in rows:
        value = row.get("selected_probability")
        if (
            type(value) not in {int, float}
            or not math.isfinite(value)
            or not 0 <= value <= 1
            or type(row.get("correct")) is not bool
        ):
            raise ValueError(
                "validation rows need selected probability and human correctness"
            )
    best = None
    for threshold in (0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        selected = [row for row in rows if row["selected_probability"] >= threshold]
        coverage = len(selected) / len(rows)
        precision = (
            sum(row["correct"] for row in selected) / len(selected) if selected else 0
        )
        candidate = (coverage >= minimum_coverage, precision, threshold)
        if best is None or candidate > best:
            best = candidate
    policy = {
        "contract": "noesis-jev-categorical-policy-v1",
        "task": task,
        "model_version": model_version,
        "rubric_version": rubric_version,
        "threshold": best[2],
        "minimum_coverage": minimum_coverage,
        "validation_ids": sorted(row["id"] for row in rows),
        "validation_groups": sorted({row["group_id"] for row in rows}),
        "validation_sha256": _hash(rows),
        "label_origins": sorted({row["label_origin"] for row in rows}),
        "task_ready": False,
    }
    policy["policy_sha256"] = _hash(policy)
    return policy


def validate_categorical_acceptance_policy(
    policy: Mapping[str, Any],
    *,
    task: str,
    model_version: str | None = None,
    rubric_version: str | None = None,
) -> None:
    threshold = policy.get("threshold")
    minimum_coverage = policy.get("minimum_coverage")
    if (
        policy.get("contract") != "noesis-jev-categorical-policy-v1"
        or _hash(
            {key: value for key, value in policy.items() if key != "policy_sha256"}
        )
        != policy.get("policy_sha256")
        or policy.get("task") != task
        or model_version is not None
        and policy.get("model_version") != model_version
        or rubric_version is not None
        and policy.get("rubric_version") != rubric_version
        or not _finite_probability(threshold)
        or not _finite_probability(minimum_coverage)
    ):
        raise ValueError("changed or mismatched categorical policy")


def _finite_probability(value: Any) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def evaluate_claim_presence(
    test_rows: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]
) -> dict:
    """Report binary precision/recall/F1 across content types and case kinds."""
    validate_claim_presence_policy(policy)
    rows = _rows(test_rows, "test")
    if {row["id"] for row in rows} & set(policy["validation_ids"]) or {
        row["group_id"] for row in rows
    } & set(policy["validation_groups"]):
        raise ValueError("related validation and test cases overlap")
    truth, prediction = [], []
    for row in rows:
        if row.get("truth") not in {"claim", "nonclaim"} or row.get("status") not in {
            "completed",
            "abstained",
            "unavailable",
            "failed",
        }:
            raise ValueError("explicit gold and result status required")
        p = row.get("p_claim")
        if row["status"] == "completed":
            if type(p) not in {int, float} or not math.isfinite(p) or not 0 <= p <= 1:
                raise ValueError("completed case needs valid p_claim")
            selected = (
                "claim"
                if p >= policy["positive_threshold"]
                else "nonclaim"
                if p <= policy["negative_threshold"]
                else None
            )
        else:
            if p is not None:
                raise ValueError("noncompleted case cannot carry p_claim")
            selected = None
        truth.append({row["truth"]})
        prediction.append({selected} if selected else set())
    labels = ["claim", "nonclaim"]
    metric = prf(truth, prediction, labels)
    strata = {}
    for field in ("source_type", "language", "case_kind"):
        strata[field] = {}
        for value in sorted({row[field] for row in rows}):
            selected = [i for i, row in enumerate(rows) if row[field] == value]
            strata[field][value] = prf(
                [truth[i] for i in selected], [prediction[i] for i in selected], labels
            )
    return {
        "contract": "noesis-jev-claim-presence-evaluation-v1",
        "n": len(rows),
        "policy_sha256": policy["policy_sha256"],
        "macro_f1": metric["macro_f1"],
        "per_class": metric["per_class"],
        "strata": strata,
        "accepted_coverage": sum(bool(value) for value in prediction) / len(rows),
        "label_origins": sorted({row["label_origin"] for row in rows}),
        "task_ready": False,
    }


def evaluate_checkworthiness(
    cases: Sequence[Mapping[str, Any]], *, budget: int
) -> dict:
    """Compare Jev and existing priority order under one fixed checking budget."""
    rows = _rows(cases, "test")
    if type(budget) is not int or not 1 <= budget <= len(rows):
        raise ValueError("fixed budget within test set required")
    for row in rows:
        if (
            type(row.get("gold_priority")) is not int
            or not 0 <= row["gold_priority"] <= 3
            or type(row.get("baseline_score")) not in {int, float}
            or row.get("jev_score") is not None
            and (
                type(row["jev_score"]) not in {int, float}
                or not 0 <= row["jev_score"] <= 1
            )
        ):
            raise ValueError("gold priority and bounded scores required")

    def quality(key: str) -> dict:
        ordered = sorted(
            rows,
            key=lambda row: (-(row[key] if row[key] is not None else -1), row["id"]),
        )[:budget]
        high = {row["id"] for row in rows if row["gold_priority"] >= 2}
        hits = sum(row["id"] in high for row in ordered)
        return {
            "high_priority_precision": hits / budget,
            "high_priority_recall": hits / len(high) if high else 1.0,
        }

    return {
        "contract": "noesis-jev-checkworthiness-evaluation-v1",
        "n": len(rows),
        "budget": budget,
        "jev": quality("jev_score"),
        "baseline": quality("baseline_score"),
        "jev_coverage": sum(row["jev_score"] is not None for row in rows) / len(rows),
        "label_origins": sorted({row["label_origin"] for row in rows}),
        "task_ready": False,
    }


def evaluate_categorical_nlp(cases: Sequence[Mapping[str, Any]], *, task: str) -> dict:
    """Stratified sentiment/attribution precision with explicit abstentions."""
    if task not in {"sentiment", "attribution"}:
        raise ValueError("supported categorical NLP task required")
    rows = _rows(cases, "test")
    labels = (
        {"positive", "negative", "neutral", "mixed"} if task == "sentiment" else None
    )
    for row in rows:
        if task == "sentiment":
            if row.get("truth") not in labels or row.get("prediction") not in labels | {
                None
            }:
                raise ValueError("sentiment gold/prediction outside ontology")
        else:
            candidates = set(row.get("candidate_ids", []))
            if (
                not candidates
                or row.get("truth") not in candidates | {"none", "uncertain"}
                or row.get("prediction") not in candidates | {"none", "uncertain", None}
            ):
                raise ValueError("attribution cannot invent a candidate")

    def summary(group: list[dict]) -> dict:
        selected = [
            row for row in group if row["prediction"] not in {None, "uncertain"}
        ]
        correct = sum(row["prediction"] == row["truth"] for row in selected)
        return {
            "n": len(group),
            "coverage": len(selected) / len(group),
            "precision": correct / len(selected) if selected else None,
            "accuracy_all": correct / len(group),
        }

    return {
        "contract": "noesis-jev-categorical-nlp-evaluation-v1",
        "task": task,
        "overall": summary(rows),
        "strata": {
            field: {
                value: summary([row for row in rows if row[field] == value])
                for value in sorted({row[field] for row in rows})
            }
            for field in ("source_type", "language", "case_kind")
        },
        "label_origins": sorted({row["label_origin"] for row in rows}),
        "task_ready": False,
    }
