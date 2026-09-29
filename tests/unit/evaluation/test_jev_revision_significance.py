from src.evaluation.jev_revision_significance import evaluate_revision_significance


def test_substantive_recall_cosmetic_false_positives_and_abstention():
    result = evaluate_revision_significance([
        {"label_origin": "fixture", "before_revision_id": "a", "after_revision_id": "b",
         "substantive_change": True, "semantic_score": 3, "coverage_complete": True},
        {"label_origin": "fixture", "before_revision_id": "b", "after_revision_id": "c",
         "substantive_change": False, "semantic_score": 0, "coverage_complete": True},
        {"label_origin": "fixture", "before_revision_id": "c", "after_revision_id": "d",
         "substantive_change": True, "semantic_score": None, "coverage_complete": False},
    ])
    assert result["substantive_recall"] == 0.5
    assert result["cosmetic_false_positive_rate"] == 0
    assert result["coverage"] == 2 / 3
    assert not result["automatic_change_brief_selection_enabled"]
