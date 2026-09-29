import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.evaluation.jev_epistemic import evaluate_jev_epistemic

ROOT = Path(__file__).resolve().parents[3]


def test_epistemic_evaluation_compares_rules_and_keeps_failures_in_denominator():
    rows = [
        {
            "id": "statement-1",
            "group_id": "source-1",
            "split": "test",
            "label_origin": "fixture",
            "statement": "According to the filing, revenue rose.",
            "truth": "report",
            "status": "completed",
            "jev_prediction": "report",
            "source_type": "news",
            "language": "en",
            "phenomena": ["attribution"],
        },
        {
            "id": "statement-2",
            "group_id": "source-2",
            "split": "test",
            "label_origin": "fixture",
            "statement": 'The minister called it "an alleged fraud".',
            "truth": "allegation",
            "status": "completed",
            "jev_prediction": "allegation",
            "source_type": "transcript",
            "language": "en",
            "phenomena": ["quoted_allegation"],
        },
        {
            "id": "statement-3",
            "group_id": "source-3",
            "split": "test",
            "label_origin": "fixture",
            "statement": "Output may decline next year.",
            "truth": "forecast",
            "status": "unavailable",
            "jev_prediction": None,
            "source_type": "paper",
            "language": "en",
            "phenomena": ["hedging"],
        },
    ]

    report = evaluate_jev_epistemic(rows)

    schema = json.loads(
        (
            ROOT
            / "contracts/schemas/jsonschema/noesis-jev-epistemic-evaluation-v1.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(report)
    assert report["cases"]["cases"] == 3
    assert report["cases"]["independent_human_cases"] == 0
    assert report["cases"]["jev"]["coverage"] == pytest.approx(2 / 3)
    assert report["cases"]["rules"]["coverage"] == 1
    assert report["statuses"]["unavailable"] == 1
    assert report["covered_human_phenomena"] == []
    assert report["task_ready"] is False


def test_epistemic_evaluation_rejects_non_test_and_unbound_case_rows():
    row = {
        "id": "statement-1",
        "group_id": "source-1",
        "split": "validation",
        "label_origin": "fixture",
        "statement": "Revenue rose.",
        "truth": "fact",
        "status": "completed",
        "jev_prediction": "fact",
        "source_type": "news",
        "language": "en",
        "phenomena": [],
    }
    with pytest.raises(ValueError, match="held-out"):
        evaluate_jev_epistemic([row])

    row["split"] = "test"
    row["jev_prediction"] = None
    with pytest.raises(ValueError, match="completed result"):
        evaluate_jev_epistemic([row])
