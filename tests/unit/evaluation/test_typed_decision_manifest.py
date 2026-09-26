from __future__ import annotations

import json
import sys
import pytest

from src.evaluation.typed_decisions import (
    evaluate_acceptance_policy,
    fit_acceptance_policy,
)
from src.evaluation.typed_decision_manifest import (
    CONTENT_TYPES,
    CASE_KINDS,
    LENGTH_BUCKETS,
)


MANIFEST = {
    "content_types": list(CONTENT_TYPES),
    "languages": ["en", "de"],
    "length_buckets": list(LENGTH_BUCKETS),
    "case_kinds": list(CASE_KINDS),
    "min_label_support": 1,
}
CRITERIA = {
    "min_macro_f1": 0.5,
    "min_accepted_coverage": 0.8,
    "max_critical_error_rate": 0.2,
}


def cases(split):
    rows = []
    for index, content_type in enumerate(CONTENT_TYPES):
        truth = "support" if index % 2 else "neutral"
        rows.append(
            {
                "id": f"{split}-{index}",
                "group_id": f"{split}-group-{index}",
                "split": split,
                "truth": truth,
                "label_origin": "fixture",
                "source": f"source-{index}",
                "domain": "news",
                "language": "en" if index % 2 else "de",
                "content_type": content_type,
                "length_bucket": LENGTH_BUCKETS[index % 3],
                "case_kind": CASE_KINDS[index % 4],
                "model_version": "jev-1.13",
                "rubric_version": "stance-v1",
                "source_binding": [
                    {
                        "kind": "document_revision",
                        "document_id": f"doc-{split}-{index}",
                        "revision_id": "rev-1",
                        "content_hash": f"hash-{split}-{index}",
                    }
                ],
                "status": "completed",
                "probabilities": {
                    "support": 0.8 if truth == "support" else 0.2,
                    "neutral": 0.8 if truth == "neutral" else 0.2,
                },
                "baseline_predictions": {
                    "local_default": {"status": "completed", "prediction": truth},
                    "calibrated_full_window": {
                        "status": "completed",
                        "prediction": truth,
                    },
                },
                "latency_ms": 10 + index,
                "input_tokens": 100,
                "total_cost_usd_micros": 10,
            }
        )
    return rows


def test_manifest_checks_coverage_provenance_and_named_baselines():
    validation, test = cases("validation"), cases("test")
    policy = fit_acceptance_policy(
        validation,
        labels=["support", "neutral"],
        task="stance",
        model_version="jev-1.13",
        rubric_version="stance-v1",
        release_criteria=CRITERIA,
        coverage_requirements=MANIFEST,
    )
    assert policy["validation_coverage"]["complete"] is True
    report = evaluate_acceptance_policy(test, policy)
    assert report["benchmark_coverage"]["test"]["complete"] is True
    assert report["named_baselines"]["local_default"]["macro_f1"] == 1
    assert report["named_baselines"]["calibrated_full_window"]["macro_f1"] == 1
    assert report["release_criteria_passed"] is False  # fixtures cannot release a task
    assert report["total_retry_inclusive_cost_usd_micros"] == 60


def test_manifest_rejects_identity_drift_and_reports_missing_strata():
    validation, test = cases("validation"), cases("test")
    policy = fit_acceptance_policy(
        validation,
        labels=["support", "neutral"],
        task="stance",
        model_version="jev-1.13",
        rubric_version="stance-v1",
        release_criteria=CRITERIA,
        coverage_requirements=MANIFEST,
    )
    changed = [dict(row) for row in test]
    changed[0]["model_version"] = "other"
    with pytest.raises(ValueError, match="model or rubric"):
        evaluate_acceptance_policy(changed, policy)
    changed[0]["model_version"] = "jev-1.13"
    changed[0]["source_binding"] = [
        {"kind": "document_revision", "document_id": "doc", "revision_id": "rev"}
    ]
    with pytest.raises(ValueError, match="content hash"):
        evaluate_acceptance_policy(changed, policy)
    partial = test[:-1]
    report = evaluate_acceptance_policy(partial, policy)
    assert report["benchmark_coverage"]["test"]["complete"] is False
    assert report["benchmark_coverage"]["test"]["missing"]["content_types"] == ["note"]
    assert report["release_criteria_passed"] is False


def test_offline_runner_writes_new_report_without_credentials(tmp_path, monkeypatch):
    from scripts.evaluate_typed_decisions import main

    paths = {
        name: tmp_path / f"{name}.json"
        for name in ("validation", "test", "criteria", "coverage")
    }
    for name, payload in (
        ("validation", cases("validation")),
        ("test", cases("test")),
        ("criteria", CRITERIA),
        ("coverage", MANIFEST),
    ):
        paths[name].write_text(json.dumps(payload))
    output = tmp_path / "report.json"
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_typed_decisions.py",
            str(paths["validation"]),
            str(paths["test"]),
            "--task",
            "stance",
            "--labels",
            "support",
            "neutral",
            "--model-version",
            "jev-1.13",
            "--rubric-version",
            "stance-v1",
            "--release-criteria",
            str(paths["criteria"]),
            "--coverage-requirements",
            str(paths["coverage"]),
            "--output",
            str(output),
        ],
    )
    assert main() == 0
    report = json.loads(output.read_text())
    assert report["held_out"]["benchmark_coverage"]["test"]["complete"] is True
    with pytest.raises(FileExistsError):
        main()
