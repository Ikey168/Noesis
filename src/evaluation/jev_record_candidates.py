"""Held-out, task-separated metrics for record-backed Jev suggestions."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence


def evaluate_identity_candidates(rows: Sequence[Mapping], *, task: str) -> dict:
    if task not in {"entity_identity", "source_identity"} or not isinstance(rows, Sequence) or not 1 <= len(rows) <= 10_000:
        raise ValueError("bounded entity or source identity cases required")
    seen, counts = set(), Counter()
    strata = {}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("task") != task:
            raise ValueError("cases must match the requested identity task")
        identity = row.get("case_id")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("unique case IDs required")
        seen.add(identity)
        if row.get("label_origin") not in {"independent-human", "fixture"}:
            raise ValueError("independent human or explicit fixture labels required")
        if row.get("challenge") not in {"homonym", "alias", "similar_name", "affiliation_change", "multilingual", "syndication", "rebrand", "shared_domain", "other"}:
            raise ValueError("declared identity challenge required")
        candidates = row.get("candidate_ids")
        truth, selected = row.get("true_candidate_id"), row.get("selected_candidate_id")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 10 or len(set(candidates)) != len(candidates):
            raise ValueError("bounded existing candidate IDs required")
        if truth is not None and truth not in candidates or selected is not None and selected not in candidates:
            raise ValueError("truth and selection must name supplied candidates or none")
        if row.get("source_version") in (None, ""):
            raise ValueError("exact candidate source version required")
        counts["cases"] += 1
        counts["correct"] += selected == truth
        counts["false_merges"] += selected is not None and selected != truth
        counts["misses"] += selected is None and truth is not None
        counts["abstentions"] += selected is None
        challenge = strata.setdefault(row["challenge"], Counter())
        challenge["cases"] += 1
        challenge["false_merges"] += selected is not None and selected != truth
    return {"contract": "noesis-jev-identity-evaluation-v1", "task": task,
            "cases": counts["cases"], "accuracy": counts["correct"] / counts["cases"],
            "false_merges": counts["false_merges"], "false_merge_rate": counts["false_merges"] / counts["cases"],
            "misses": counts["misses"], "abstention_rate": counts["abstentions"] / counts["cases"],
            "by_challenge": {key: dict(value) for key, value in sorted(strata.items())},
            "label_origins": sorted({row["label_origin"] for row in rows}),
            "automatic_merge_enabled": False}


def evaluate_methodology_categories(rows: Sequence[Mapping]) -> dict:
    if not isinstance(rows, Sequence) or not 1 <= len(rows) <= 10_000:
        raise ValueError("bounded methodology cases required")
    seen, counts = set(), Counter()
    per_field = {field: Counter() for field in ("study_design", "method", "limitation")}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("label_origin") not in {"independent-human", "fixture"}:
            raise ValueError("independent human or explicit fixture labels required")
        identity = row.get("statement_id")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("unique statement IDs required")
        seen.add(identity)
        if row.get("study_revision_id") in (None, "") or row.get("source_revision_id") in (None, ""):
            raise ValueError("study and source revisions required")
        truth, suggestion = row.get("truth"), row.get("suggestion")
        if not isinstance(truth, Mapping) or not isinstance(suggestion, Mapping):
            raise ValueError("human and machine category maps required")
        counts["cases"] += 1
        for field, bucket in per_field.items():
            expected, proposed = truth.get(field), suggestion.get(field)
            if not isinstance(expected, str) or not expected or proposed is not None and not isinstance(proposed, str):
                raise ValueError("valid category labels required")
            bucket["cases"] += 1
            bucket["correct"] += proposed == expected
            bucket["abstained"] += proposed is None
    return {"contract": "noesis-jev-methodology-evaluation-v1", "cases": counts["cases"],
            "fields": {field: {"accuracy": bucket["correct"] / bucket["cases"],
                               "abstention_rate": bucket["abstained"] / bucket["cases"]}
                       for field, bucket in per_field.items()},
            "label_origins": sorted({row["label_origin"] for row in rows}),
            "automatic_category_acceptance_enabled": False}
