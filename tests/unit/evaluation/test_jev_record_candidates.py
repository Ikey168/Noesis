from src.evaluation.jev_record_candidates import (
    evaluate_identity_candidates, evaluate_methodology_categories,
)


def test_identity_evaluation_counts_homonym_false_merge_without_enabling_merge():
    result = evaluate_identity_candidates([
        {"case_id": "h1", "task": "entity_identity", "label_origin": "independent-human",
         "challenge": "homonym", "candidate_ids": ["a"], "true_candidate_id": None,
         "selected_candidate_id": "a", "source_version": "v1"},
        {"case_id": "a1", "task": "entity_identity", "label_origin": "independent-human",
         "challenge": "alias", "candidate_ids": ["b"], "true_candidate_id": "b",
         "selected_candidate_id": None, "source_version": "v2"},
        {"case_id": "affiliation-change", "task": "entity_identity", "label_origin": "fixture",
         "challenge": "affiliation_change", "candidate_ids": ["c"], "true_candidate_id": None,
         "selected_candidate_id": "c", "source_version": "v3"},
        {"case_id": "multilingual-name", "task": "entity_identity", "label_origin": "fixture",
         "challenge": "multilingual", "candidate_ids": ["d"], "true_candidate_id": "d",
         "selected_candidate_id": "d", "source_version": "v4"},
    ], task="entity_identity")
    assert result["false_merges"] == 2 and result["false_merge_rate"] == 0.5
    assert result["by_challenge"]["homonym"]["false_merges"] == 1
    assert result["by_challenge"]["affiliation_change"]["false_merges"] == 1
    assert result["by_challenge"]["multilingual"]["cases"] == 1
    assert not result["automatic_merge_enabled"]


def test_source_identity_evaluation_stratifies_publisher_alias_challenges():
    challenges = [
        ("publisher-similar-names", "similar_name"),
        ("wire-copy-syndication", "syndication"),
        ("publisher-rebrand", "rebrand"),
        ("shared-publisher-domain", "shared_domain"),
    ]
    rows = [
        {"case_id": case_id, "task": "source_identity", "label_origin": "fixture",
         "challenge": challenge, "candidate_ids": [f"candidate-{index}"],
         "true_candidate_id": None if index != 2 else f"candidate-{index}",
         "selected_candidate_id": f"candidate-{index}" if index != 2 else f"candidate-{index}",
         "source_version": f"source-revision-{index}"}
        for index, (case_id, challenge) in enumerate(challenges)
    ]
    result = evaluate_identity_candidates(rows, task="source_identity")
    assert result["cases"] == 4 and result["false_merges"] == 3
    assert {"similar_name", "syndication", "rebrand", "shared_domain"} <= set(result["by_challenge"])
    assert not result["automatic_merge_enabled"]


def test_methodology_evaluation_keeps_missing_category_in_denominator():
    result = evaluate_methodology_categories([
        {"statement_id": "s1", "label_origin": "fixture", "study_revision_id": "study-r1",
         "source_revision_id": "doc-r1",
         "truth": {"study_design": "randomized_controlled", "method": "experimental", "limitation": "not_reported"},
         "suggestion": {"study_design": "randomized_controlled", "method": None, "limitation": "not_reported"}},
    ])
    assert result["fields"]["study_design"]["accuracy"] == 1
    assert result["fields"]["method"]["abstention_rate"] == 1
    assert not result["automatic_category_acceptance_enabled"]
