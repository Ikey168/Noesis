from src.evaluation.jev_review_priority import evaluate_review_priority


def test_fixed_budget_discovers_earlier_error_without_changing_review_queue():
    rows = [
        {"task_id": "a", "label_origin": "independent-human", "high_impact_error": False,
         "baseline_priority": 3, "suggested_priority": 1, "source_revision": "r1", "target_revision_hash": "h1"},
        {"task_id": "b", "label_origin": "independent-human", "high_impact_error": True,
         "baseline_priority": 1, "suggested_priority": 3, "source_revision": "r2", "target_revision_hash": "h2"},
    ]
    result = evaluate_review_priority(rows, reviewer_budget=1)
    assert result["found_baseline"] == 0 and result["found_suggested"] == 1
    assert result["delta_found"] == 1 and not result["automatic_priority_update_enabled"]
