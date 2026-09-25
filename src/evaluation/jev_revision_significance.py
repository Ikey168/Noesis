"""Human-labeled recall and cosmetic false-positive gate for revision advice."""

from collections.abc import Mapping, Sequence


def evaluate_revision_significance(cases: Sequence[Mapping], *, threshold: int = 2) -> dict:
    if not isinstance(cases, Sequence) or not 1 <= len(cases) <= 10_000 or type(threshold) is not int or threshold not in range(1, 4):
        raise ValueError("bounded cases and score threshold required")
    substantive = cosmetic = true_positive = false_positive = covered = 0
    origins = set()
    for case in cases:
        if not isinstance(case, Mapping) or case.get("label_origin") not in {"independent-human", "fixture"}:
            raise ValueError("independent human or explicit fixture label required")
        if type(case.get("substantive_change")) is not bool:
            raise ValueError("substantive_change boolean required")
        if not case.get("before_revision_id") or not case.get("after_revision_id"):
            raise ValueError("exact revision pair required")
        score = case.get("semantic_score")
        if score is not None and (type(score) is not int or score not in range(4)):
            raise ValueError("score must be zero through three or abstention")
        if case["substantive_change"]:
            substantive += 1
        else:
            cosmetic += 1
        if case.get("coverage_complete") is True and score is not None:
            covered += 1
            if score >= threshold:
                if case["substantive_change"]:
                    true_positive += 1
                else:
                    false_positive += 1
        origins.add(case["label_origin"])
    return {"contract": "noesis-jev-revision-significance-evaluation-v1",
            "cases": len(cases), "threshold": threshold,
            "coverage": covered / len(cases),
            "substantive_recall": true_positive / substantive if substantive else None,
            "cosmetic_false_positive_rate": false_positive / cosmetic if cosmetic else None,
            "label_origins": sorted(origins), "automatic_change_brief_selection_enabled": False}
