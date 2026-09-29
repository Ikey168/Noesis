"""Held-out evaluation for source-bound Awareness triage suggestions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

DECISIONS = {"watch", "escalate", "schedule", "discard", "archive", "flag"}


def evaluate_awareness_triage(cases: Sequence[Mapping]) -> dict:
    """Compare suggestion and keyword-preview recall against triage labels.

    “Important” is defined by the human label ``escalate`` or ``flag``. A
    keyword preview is only a match signal, not a predicted triage action.
    Fixture labels validate calculations only; they do not establish quality.
    """
    if (
        not isinstance(cases, Sequence)
        or isinstance(cases, (str, bytes))
        or not 1 <= len(cases) <= 10_000
    ):
        raise ValueError("bounded Awareness test cases required")
    item_ids, groups = set(), set()
    important_total = keyword_hits = semantic_hits = false_discards = (
        predicted_discards
    ) = correct = covered = 0
    origins = set()
    for case in cases:
        if not isinstance(case, Mapping) or case.get("split") != "test":
            raise ValueError("held-out test cases required")
        if case.get("label_origin") not in {"independent-human", "fixture"}:
            raise ValueError("human or fixture label provenance required")
        item_id, group_id = case.get("item_id"), case.get("group_id")
        if not isinstance(item_id, str) or not item_id or item_id in item_ids:
            raise ValueError("unique inbox item identities required")
        if not isinstance(group_id, str) or not group_id:
            raise ValueError("feed/source group identity required")
        item_ids.add(item_id)
        groups.add(group_id)
        origins.add(case["label_origin"])
        truth, suggestion = case.get("truth"), case.get("suggestion")
        if truth not in DECISIONS or suggestion not in DECISIONS | {"abstained"}:
            raise ValueError(
                "human label and explicit suggestion or abstention required"
            )
        keyword_match = case.get("keyword_preview_match")
        if type(keyword_match) is not bool:
            raise ValueError("keyword preview match outcome required")
        important = truth in {"escalate", "flag"}
        important_total += important
        keyword_hits += important and keyword_match
        semantic_hits += important and suggestion not in {
            "discard",
            "archive",
            "abstained",
        }
        predicted_discards += suggestion == "discard"
        false_discards += suggestion == "discard" and truth != "discard"
        covered += suggestion != "abstained"
        correct += suggestion == truth
    return {
        "contract": "noesis-jev-awareness-evaluation-v1",
        "cases": len(cases),
        "source_groups": len(groups),
        "label_origins": sorted(origins),
        "important_definition": ["escalate", "flag"],
        "important_cases": important_total,
        "keyword_preview_important_recall": keyword_hits / important_total
        if important_total
        else None,
        "semantic_important_recall": semantic_hits / important_total
        if important_total
        else None,
        "missed_important_items": important_total - semantic_hits,
        "false_discard_count": false_discards,
        "false_discard_rate": false_discards / predicted_discards
        if predicted_discards
        else None,
        "coverage": covered / len(cases),
        "accuracy_over_all_cases": correct / len(cases),
        "task_ready": False,
        "readiness_requires": "held-out independent human triage labels, missed-important/false-discard review, and measured deployment evidence",
    }
