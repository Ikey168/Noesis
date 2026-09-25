"""Fixed-budget comparison of review priority suggestions against human labels."""

from collections.abc import Mapping, Sequence


def evaluate_review_priority(rows: Sequence[Mapping], *, reviewer_budget: int) -> dict:
    if (not isinstance(rows, Sequence) or not 1 <= len(rows) <= 10_000 or
        type(reviewer_budget) is not int or not 1 <= reviewer_budget <= len(rows)):
        raise ValueError("bounded cases and fixed reviewer budget required")
    ids = set()
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("task_id"), str) or not row["task_id"] or row["task_id"] in ids:
            raise ValueError("unique review task IDs required")
        ids.add(row["task_id"])
        if row.get("label_origin") not in {"independent-human", "fixture"} or type(row.get("high_impact_error")) is not bool:
            raise ValueError("independent human or explicit fixture high-impact error label required")
        for key in ("baseline_priority", "suggested_priority"):
            value = row.get(key)
            if type(value) not in {int, float} or not 0 <= value <= 3:
                raise ValueError("bounded baseline and machine priority required")
        if row.get("source_revision") in (None, "") or row.get("target_revision_hash") in (None, ""):
            raise ValueError("exact source and target revisions required")
    baseline = sorted(rows, key=lambda row: (-row["baseline_priority"], row["task_id"]))[:reviewer_budget]
    proposed = sorted(rows, key=lambda row: (-row["suggested_priority"], row["task_id"]))[:reviewer_budget]
    found_baseline = sum(row["high_impact_error"] for row in baseline)
    found_proposed = sum(row["high_impact_error"] for row in proposed)
    return {"contract": "noesis-jev-review-priority-evaluation-v1",
            "cases": len(rows), "reviewer_budget": reviewer_budget,
            "high_impact_errors_total": sum(row["high_impact_error"] for row in rows),
            "found_baseline": found_baseline, "found_suggested": found_proposed,
            "delta_found": found_proposed - found_baseline,
            "label_origins": sorted({row["label_origin"] for row in rows}),
            "automatic_priority_update_enabled": False}
