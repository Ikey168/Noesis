from src.evaluation.jev_source_planning import evaluate_source_relevance


def test_equal_budget_yield_and_coverage_comparison():
    result = evaluate_source_relevance([{
        "label_origin": "fixture", "objective_input_hash": "objective-v1",
        "capability_version_set": ["source-v1"], "budget": 2,
        "baseline_spend": 2, "advised_spend": 2,
        "baseline_evidence_yield": 0.5, "advised_evidence_yield": 0.75,
        "baseline_objective_coverage": 0.5, "advised_objective_coverage": 1.0,
    }])
    assert result["yield_delta"] == 0.25
    assert result["coverage_delta"] == 0.5
    assert not result["automatic_source_plan_acceptance_enabled"]
