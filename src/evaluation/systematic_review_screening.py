"""Stage-separated screening metrics against independent human labels."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence


def evaluate_screening_suggestions(
    rows: Sequence[Mapping], *, stage: str, allow_fixture: bool = False
) -> dict:
    """Score retained suggestions; fixture labels exercise plumbing only.

    Pending, abstained and unavailable cases remain in the denominator. This
    evaluator never counts a machine output as a human vote or releases an
    automated exclusion policy.
    """
    if stage not in {"title_abstract", "full_text"} or not isinstance(rows, Sequence) or not 1 <= len(rows) <= 10_000:
        raise ValueError("bounded title/abstract or full-text evaluation rows required")
    ids, groups = set(), set()
    counts = Counter()
    criterion_total = criterion_correct = 0
    for row in rows:
        if not isinstance(row, Mapping) or row.get("stage") != stage:
            raise ValueError("all evaluation cases must match the requested stage")
        case_id, group_id = row.get("candidate_id"), row.get("study_id")
        if not isinstance(case_id, str) or not case_id or case_id in ids or not isinstance(group_id, str) or not group_id:
            raise ValueError("unique candidate and study identities required")
        ids.add(case_id)
        groups.add(group_id)
        origin = row.get("label_origin")
        if origin not in {"independent-human", "fixture"}:
            raise ValueError("independent human or explicit fixture labels required")
        if origin == "fixture" and not allow_fixture:
            raise ValueError("fixture labels require explicit allow_fixture=True")
        truth, suggestion = row.get("truth"), row.get("suggested_decision")
        if truth not in {"include", "exclude"} or suggestion not in {"include", "exclude", "pending"}:
            raise ValueError("resolved truth and explicit suggestion required")
        if row.get("source_revision") in (None, "") or row.get("protocol_revision") in (None, ""):
            raise ValueError("source and protocol revisions required")
        labels = row.get("criteria_truth")
        assessed = row.get("criteria_assessed")
        if not isinstance(labels, Mapping) or not isinstance(assessed, Mapping) or set(labels) != set(assessed):
            raise ValueError("criterion codes and independent labels must align")
        for code, label in labels.items():
            if not isinstance(code, str) or not code or label not in {"satisfied", "not_satisfied", "not_reported"}:
                raise ValueError("valid criterion labels required")
            prediction = assessed[code]
            if prediction not in {"satisfied", "not_satisfied", "not_reported", "abstained"}:
                raise ValueError("valid criterion assessment required")
            criterion_total += 1
            criterion_correct += prediction == label
        counts["total"] += 1
        counts["true_include"] += truth == "include"
        counts["true_exclude"] += truth == "exclude"
        counts["suggest_include"] += suggestion == "include"
        counts["suggest_exclude"] += suggestion == "exclude"
        counts["pending"] += suggestion == "pending"
        counts["true_include_suggest_include"] += truth == "include" and suggestion == "include"
        counts["false_exclusion"] += truth == "include" and suggestion == "exclude"
        counts["false_inclusion"] += truth == "exclude" and suggestion == "include"
    return {
        "contract": "noesis-review-screening-evaluation-v1", "stage": stage,
        "cases": counts["total"], "studies": len(groups),
        "label_origins": sorted({row["label_origin"] for row in rows}),
        "inclusion_recall": (counts["true_include_suggest_include"] / counts["true_include"]
                             if counts["true_include"] else None),
        "false_exclusion_rate": (counts["false_exclusion"] / counts["true_include"]
                                 if counts["true_include"] else None),
        "false_exclusions": counts["false_exclusion"],
        "false_inclusions": counts["false_inclusion"],
        "pending_rate": counts["pending"] / counts["total"],
        "criterion_accuracy": criterion_correct / criterion_total if criterion_total else None,
        "criterion_cases": criterion_total,
        "automated_exclusion_enabled": False,
        "task_ready": False,
        "readiness_requires": "stage-specific independent human labels and predeclared false-exclusion limits",
    }
