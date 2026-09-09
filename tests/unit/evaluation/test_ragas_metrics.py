import asyncio

import pytest

from src.evaluation.ragas_metrics import evaluate_ragas, validate_cases


def cases():
    return [
        {
            "id": "fixture",
            "question": "Where?",
            "answer": "Berlin",
            "contexts": [
                {"id": "d", "revision": "r", "text": "Berlin is the subject."}
            ],
            "citations": ["d"],
            "reference_context_ids": ["d", "missing"],
            "reference_contexts": ["Berlin is the subject."],
            "label_origin": "fixture",
        }
    ]


def test_native_ragas_metrics_do_not_require_a_hosted_judge():
    pytest.importorskip("ragas")
    result = asyncio.run(evaluate_ragas(cases()))
    assert result["status"] == "completed"
    assert result["cases"][0]["metrics"]["id_precision"]["value"] == 1
    assert result["cases"][0]["metrics"]["id_recall"]["value"] == 0.5
    assert not result["support_verified"] and result["hosted_calls"] == 0


def test_frozen_citations_and_provenance_are_required():
    values = cases()
    values[0]["citations"] = ["not captured"]
    with pytest.raises(ValueError, match="citation"):
        validate_cases(values)
    values = cases()
    values[0]["label_origin"] = "human-ish"
    with pytest.raises(ValueError, match="provenance"):
        validate_cases(values)
