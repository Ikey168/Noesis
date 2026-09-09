import copy
import json
from pathlib import Path

import pytest

from src.evaluation.fuzzy_resolution import benchmark_fuzzy_resolution, score_name_pair

FIXTURE = Path("tests/fixtures/workflow_review/fuzzy_names.json")


def test_optional_rapidfuzz_scores_without_changing_resolution_safeguards():
    assert (
        score_name_pair("organization", "Acme GmbH", "Acme", backend="rapidfuzz-ratio")
        == 1.0
    )
    report = benchmark_fuzzy_resolution(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert set(report["results"]) == {"sequence-matcher", "rapidfuzz-ratio"}
    assert report["production_default_changed"] is False
    assert report["thresholds_reused_between_metrics"] is False
    assert "exact identifiers retain precedence" in report["identity_safeguards"]
    assert report["decision"] in {"defer", "adopt-for-optional-candidate-scoring"}


def test_threshold_curve_reports_recall_false_merges_latency_and_memory():
    report = benchmark_fuzzy_resolution(
        json.loads(FIXTURE.read_text(encoding="utf-8")), [0.75, 0.9]
    )
    for result in report["results"].values():
        assert len(result["threshold_curve"]) == 2
        assert result["latency_ns_total"] >= 0
        assert result["latency_ns_per_pair"] >= 0
        assert result["peak_tracemalloc_bytes"] >= 0
        assert {"candidate_recall", "false_merge_rate"} <= set(
            result["threshold_curve"][0]
        )


def test_heldout_labels_never_select_thresholds_and_groups_cannot_leak():
    fixture = json.loads(
        Path("tests/fixtures/workflow_review/fuzzy_names_split.json").read_text()
    )
    first = benchmark_fuzzy_resolution(
        fixture["development"], test_cases=fixture["test"]
    )
    changed = copy.deepcopy(fixture["test"])
    for case in changed:
        case["same_entity"] = not case["same_entity"]
    second = benchmark_fuzzy_resolution(fixture["development"], test_cases=changed)
    for backend in first["results"]:
        assert (
            first["results"][backend]["heldout"]["threshold"]
            == second["results"][backend]["heldout"]["threshold"]
        )
        assert (
            first["results"][backend]["heldout"]["threshold_selected_on"]
            == "development"
        )
    assert first["split_sha256"]["test"] != second["split_sha256"]["test"]
    changed[0]["group_id"] = fixture["development"][0]["group_id"]
    with pytest.raises(ValueError, match="leakage"):
        benchmark_fuzzy_resolution(fixture["development"], test_cases=changed)


def test_calibration_only_cannot_justify_adoption_or_coerce_string_labels():
    cases = json.loads(FIXTURE.read_text())
    assert benchmark_fuzzy_resolution(cases)["decision"] == "defer"
    cases[0]["same_entity"] = "false"
    with pytest.raises(ValueError, match="boolean"):
        benchmark_fuzzy_resolution(cases)
