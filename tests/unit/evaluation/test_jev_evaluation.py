"""Typed-decision evaluation keeps failed cases and split leakage visible."""

import pytest

from src.evaluation.typed_decisions import (
    evaluate_acceptance_policy,
    fit_acceptance_policy,
)


def case(id, split, truth, probabilities, *, status="completed", group=None):
    return {
        "id": id, "group_id": group or id, "split": split,
        "truth": truth, "status": status,
        "probabilities": probabilities,
        "label_origin": "fixture", "source": "source-a", "domain": "news",
        "language": "en", "content_type": "article", "input_tokens": 100,
        "latency_ms": 20,
    }


def test_unavailable_stays_in_denominator_and_cost_is_reported():
    validation = [
        case("v1", "validation", "yes", {"yes": 0.9, "no": 0.1}),
        case("v2", "validation", "no", {"yes": 0.1, "no": 0.9}),
    ]
    policy = fit_acceptance_policy(
        validation, labels=["yes", "no"], task="relevance",
        model_version="jev-1.13.0", rubric_version="r1",
    )
    test = [
        case("t1", "test", "yes", {"yes": 0.95, "no": 0.05}),
        case("t2", "test", "no", None, status="unavailable"),
    ]
    result = evaluate_acceptance_policy(test, policy)
    assert result["n"] == 2
    assert result["statuses"]["unavailable"] == 1
    assert result["accepted_coverage"] == 0.5
    assert result["accuracy_all_cases"] == 0.5
    assert result["estimated_input_usd"] > 0
    assert result["task_ready"] is False


def test_related_document_group_cannot_cross_splits():
    policy = fit_acceptance_policy(
        [case("v", "validation", "yes", {"yes": 0.7, "no": 0.3}, group="same")],
        labels=["yes", "no"], task="screening",
        model_version="jev-1.13.0", rubric_version="r1",
    )
    with pytest.raises(ValueError, match="overlap"):
        evaluate_acceptance_policy(
            [case("t", "test", "no", {"yes": 0.2, "no": 0.8}, group="same")],
            policy,
        )


def test_invalid_distribution_is_rejected():
    with pytest.raises(ValueError, match="distribution"):
        fit_acceptance_policy(
            [case("v", "validation", "yes", {"yes": 1.2, "no": -0.2})],
            labels=["yes", "no"], task="relevance",
            model_version="jev-1.13.0", rubric_version="r1",
        )


def test_baseline_uses_same_held_out_cases_and_retry_costs():
    policy = fit_acceptance_policy(
        [case("v", "validation", "yes", {"yes": 0.8, "no": 0.2})],
        labels=["yes", "no"], task="relevance",
        model_version="jev-1.13.0", rubric_version="r1",
    )
    rows = [
        {**case("t1", "test", "yes", {"yes": 0.9, "no": 0.1}),
         "baseline_status": "completed", "baseline_prediction": "no",
         "total_cost_usd_micros": 14},
        {**case("t2", "test", "no", None, status="unavailable"),
         "baseline_status": "completed", "baseline_prediction": "no",
         "total_cost_usd_micros": 7},
    ]
    result = evaluate_acceptance_policy(rows, policy)
    assert result["baseline_macro_f1"] is not None
    assert result["total_retry_inclusive_cost_usd_micros"] == 21
    assert result["cost_coverage"] == 1
    assert result["strata"]["content_type"]["article"]["n"] == 2
    with pytest.raises(ValueError, match="baseline must cover"):
        evaluate_acceptance_policy(
            [{key: value for key, value in rows[0].items()
              if not key.startswith("baseline_")}, rows[1]], policy,
        )


def test_predeclared_release_gates_do_not_promote_fixture_labels():
    policy = fit_acceptance_policy(
        [case("v", "validation", "yes", {"yes": 0.99, "no": 0.01})],
        labels=["yes", "no"], task="relevance",
        model_version="jev-1.13.0", rubric_version="r1",
        release_criteria={"min_macro_f1": 0.5, "max_critical_error_rate": 0},
    )
    result = evaluate_acceptance_policy(
        [case("t", "test", "yes", {"yes": 0.99, "no": 0.01})], policy,
    )
    assert all(gate["passed"] for gate in result["release_gates"].values())
    assert result["release_criteria_passed"] is False
    assert result["task_ready"] is False
