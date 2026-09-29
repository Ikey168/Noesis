"""Held-out comparison of Jev statement-kind suggestions with local rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from src.argument_mining.model_diagnostics import prf
from src.kb.epistemic import STATUSES, classify_statement

REQUIRED_PHENOMENA = {"attribution", "hedging", "quoted_allegation"}
RESULT_STATUSES = {"completed", "abstained", "unavailable", "failed"}


def _scores(
    rows: Sequence[Mapping], labels: Sequence[str] = STATUSES
) -> dict:
    truth = [{row["truth"]} for row in rows]
    predictions = [
        {row["jev_prediction"]} if row["status"] == "completed" else set()
        for row in rows
    ]
    rules = [{classify_statement(row["statement"])["status"]} for row in rows]
    jev_metrics = prf(truth, predictions, labels)
    rule_metrics = prf(truth, rules, labels)
    return {
        "cases": len(rows),
        "independent_human_cases": sum(
            row["label_origin"] == "independent-human" for row in rows
        ),
        "jev": {
            "macro_f1": jev_metrics["macro_f1"],
            "per_label": jev_metrics["per_class"],
            "accuracy": jev_metrics["exact_accuracy"],
            "coverage": sum(row["status"] == "completed" for row in rows)
            / len(rows),
        },
        "rules": {
            "macro_f1": rule_metrics["macro_f1"],
            "per_label": rule_metrics["per_class"],
            "accuracy": rule_metrics["exact_accuracy"],
            "coverage": 1.0,
        },
        "jev_minus_rules_macro_f1": (
            jev_metrics["macro_f1"] - rule_metrics["macro_f1"]
        ),
    }


def evaluate_jev_epistemic(test_cases: Sequence[Mapping]) -> dict:
    """Compare frozen Jev suggestions and regex rules against held-out labels.

    Failed and abstained calls remain in the evaluation denominator. Fixture
    labels are useful for checking the report shape, but cannot establish
    readiness or substitute for independently labeled statements.
    """
    if (
        not isinstance(test_cases, Sequence)
        or isinstance(test_cases, (str, bytes))
        or not 1 <= len(test_cases) <= 10_000
    ):
        raise ValueError("bounded epistemic test cases required")

    clean: list[dict] = []
    seen: set[str] = set()
    for row in test_cases:
        if not isinstance(row, Mapping) or row.get("split") != "test":
            raise ValueError("held-out test cases required")
        case_id, group_id = row.get("id"), row.get("group_id")
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in seen
            or not isinstance(group_id, str)
            or not group_id
        ):
            raise ValueError("unique statement and source-group identities required")
        if row.get("label_origin") not in {
            "independent-human",
            "assisted-human",
            "fixture",
        }:
            raise ValueError("explicit human or fixture label provenance required")
        statement = row.get("statement")
        if not isinstance(statement, str) or not statement.strip() or len(statement) > 8_000:
            raise ValueError("bounded statement text required")
        if row.get("truth") not in STATUSES:
            raise ValueError("human statement-kind label is outside the taxonomy")
        status = row.get("status")
        if status not in RESULT_STATUSES:
            raise ValueError("explicit Jev result status required")
        prediction = row.get("jev_prediction")
        if status == "completed":
            if prediction not in STATUSES:
                raise ValueError("completed result requires a statement-kind label")
        elif prediction is not None:
            raise ValueError("unavailable or abstained result cannot carry a label")
        source_type = row.get("source_type")
        language = row.get("language")
        if not isinstance(source_type, str) or not source_type:
            raise ValueError("source content type required")
        if not isinstance(language, str) or not language:
            raise ValueError("statement language required")
        phenomena = row.get("phenomena", [])
        if not isinstance(phenomena, list) or any(
            not isinstance(value, str) or not value for value in phenomena
        ):
            raise ValueError("phenomena must be a list of named case properties")
        clean.append(dict(row))
        seen.add(case_id)

    human_rows = [
        row for row in clean if row["label_origin"] == "independent-human"
    ]
    human_phenomena = {
        phenomenon
        for row in human_rows
        for phenomenon in row.get("phenomena", [])
    }
    by_content_type = {
        content_type: _scores(
            [row for row in clean if row["source_type"] == content_type]
        )
        for content_type in sorted({row["source_type"] for row in clean})
    }
    by_language = {
        language: _scores([row for row in clean if row["language"] == language])
        for language in sorted({row["language"] for row in clean})
    }
    by_phenomenon = {
        phenomenon: _scores(
            [row for row in human_rows if phenomenon in row.get("phenomena", [])]
        )
        for phenomenon in sorted(human_phenomena)
    }
    statuses = {
        status: sum(row["status"] == status for row in clean)
        for status in sorted(RESULT_STATUSES)
    }
    return {
        "contract": "noesis-jev-epistemic-evaluation-v1",
        "cases": _scores(clean),
        "independent_human_cases": len(human_rows),
        "required_human_phenomena": sorted(REQUIRED_PHENOMENA),
        "covered_human_phenomena": sorted(human_phenomena & REQUIRED_PHENOMENA),
        "by_content_type": by_content_type,
        "by_language": by_language,
        "by_phenomenon": by_phenomenon,
        "statuses": statuses,
        "label_origins": sorted({row["label_origin"] for row in clean}),
        "task_ready": False,
        "readiness_requires": (
            "independent human-labeled held-out statements covering attribution, "
            "hedging and quoted allegations, plus measured quality and domain review"
        ),
    }
