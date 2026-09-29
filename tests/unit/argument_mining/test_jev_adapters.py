from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.argument_mining.jev_adapters import (
    FRAME_DESCRIPTIONS,
    STANCE_LABELS,
    suggest_frames,
    suggest_stance,
)
from src.evaluation.jev_mining import evaluate_jev_mining, fit_jev_mining_policy


REF = {"document_id": "doc", "revision_id": "rev"}
SCOPES = {"document:doc:read"}


def _validate_evaluation(report):
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema/"
            "noesis-jev-mining-evaluation-v1.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(report)


class FakeRuntime:
    def __init__(self, text, fail_window=None):
        self.text, self.fail_window, self.calls = text, fail_window, []

    def capture_sources(self, namespace, principal_id, source_refs, scopes):
        assert source_refs == [REF] and "document:doc:read" in scopes
        return ([{"kind": "document_revision", **REF}], [{"content": self.text}])

    def run(self, namespace, run_id, task, *, state, questions, **kwargs):
        self.calls.append((run_id, task, state, questions))
        if self.fail_window is not None and run_id.endswith(
            f":window:{self.fail_window}"
        ):
            return {
                "status": "unavailable",
                "rollout_mode": "manual",
                "receipt": None,
                "source_binding": [{"revision_id": "rev"}],
            }
        if task == "jev-stance-v1":
            scores = {
                "supportive": 0.05,
                "critical": 0.05,
                "neutral": 0.1,
                "ambiguous": 0.8,
            }
            answers = {
                "stance": {
                    "kind": "choice",
                    "status": "answered",
                    "value": "ambiguous",
                    "probabilities": scores,
                    "selected_probability": 0.8,
                    "vendor_confidence": 0.55,
                }
            }
        else:
            window = state["window_text"].lower()
            answers = {}
            for index, label in enumerate(FRAME_DESCRIPTIONS):
                score = (
                    0.9
                    if (label == "scientific" and "experiment" in window)
                    or (label == "economic" and "economic cost" in window)
                    else 0.1
                )
                answers[f"frame_{index}"] = {
                    "kind": "noul",
                    "status": "answered",
                    "value": score,
                }
        return {
            "status": "completed",
            "rollout_mode": "manual",
            "receipt": {
                "model_requested": "jev-1.13",
                "rubric_id": "v1",
                "answers": answers,
            },
            "source_binding": [{"revision_id": "rev"}],
        }


def row(case_id, split, labels, scores, task, source_type="news"):
    result = {
        "id": case_id,
        "group_id": case_id,
        "split": split,
        "labels": labels,
        "scores": scores,
        "status": "completed",
        "label_origin": "fixture",
        "source": source_type,
        "source_type": source_type,
        "domain": "news",
        "language": "en",
    }
    if task == "frames":
        result["dominant_truth"] = labels[0] if labels else "other"
    return result


def policy(task):
    labels = list(STANCE_LABELS if task == "stance" else FRAME_DESCRIPTIONS)
    scores = (
        [0.05, 0.05, 0.1, 0.8] if task == "stance" else [0.1, 0.1, 0.1, 0.1, 0.1, 0.9]
    )
    validation = [
        row(
            "validation-1",
            "validation",
            ["ambiguous"] if task == "stance" else ["scientific"],
            scores,
            task,
        )
    ]
    return fit_jev_mining_policy(
        validation,
        task=task,
        labels=labels,
        model_version="jev-1.13",
        rubric_version="v1",
    )


def test_stance_preserves_source_span_and_topic_context():
    text = (
        "The minister endorsed the proposal without reservations. "
        'A witness said, "The opposition rejects the proposal." '
        "The reporter did not endorse either position."
    )
    runtime = FakeRuntime(text)
    result = suggest_stance(
        runtime,
        "ns",
        "stance-1",
        REF,
        topic="the proposal",
        sentence_index=1,
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy={},
        calibration_policy=policy("stance"),
    )
    assert result["text"] == text[result["span"]["start"] : result["span"]["end"]]
    assert result["sentence_index"] == 1 and result["topic"] == "the proposal"
    assert result["candidate"] == result["suggested_stance"] == "ambiguous"
    assert result["accepted"] is False
    assert "witness" in runtime.calls[0][2]["bounded_context"]
    assert runtime.calls[0][2]["sentence_span"] == list(result["span"].values())


def test_stance_calibration_requires_matching_rubric_receipt():
    class ChangedRubricRuntime(FakeRuntime):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            result["receipt"]["rubric_id"] = "different-rubric"
            return result

    with pytest.raises(ValueError, match="template changed"):
        suggest_stance(
            ChangedRubricRuntime("The proposal was supported."),
            "ns",
            "stance-rubric-drift",
            REF,
            topic="the proposal",
            sentence_index=0,
            principal_id="alice",
            scopes=SCOPES,
            allow_remote=True,
            policy={},
            calibration_policy=policy("stance"),
        )


@pytest.mark.parametrize(
    "topic,index,expected",
    [
        ("the proposed law", 0, "Speaker A"),
        ("the proposed law", 1, "Speaker B"),
        ("the proposed law", 2, "Its likely effect"),
        ("the funding bill", 3, "The debate shifted"),
    ],
)
def test_stance_context_cases_keep_exact_target_and_topic(topic, index, expected):
    text = (
        "Speaker A said the proposed law would help residents. "
        "Speaker B said the proposed law would harm residents. "
        "Its likely effect on costs was left unstated. "
        "The debate shifted to the funding bill, which no speaker endorsed."
    )
    runtime = FakeRuntime(text)
    result = suggest_stance(
        runtime,
        "ns",
        f"stance-{index}",
        REF,
        topic=topic,
        sentence_index=index,
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy={},
    )
    assert result["text"].startswith(expected)
    assert runtime.calls[0][2]["topic"] == topic
    assert runtime.calls[0][2]["sentence_text"] == result["text"]
    assert result["suggested_stance"] is None  # no human-fitted policy


def test_frames_cover_tail_and_keep_independent_labels():
    text = "Routine account. " * 350 + "A controlled experiment tested the result."
    runtime = FakeRuntime(text)
    result = suggest_frames(
        runtime,
        "ns",
        "frames-1",
        REF,
        document_kind="news",
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy={},
        calibration_policy=policy("frames"),
    )
    assert len(runtime.calls) > 1
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["covered_chars"] == len(text)
    assert result["raw_scores"]["scientific"] == 0.9
    assert result["suggested_frames"] == ["scientific"]
    assert result["dominant"] == "scientific"
    assert "other" not in runtime.calls[0][2]["frame_labels"]


def test_incomplete_coverage_abstains_without_partial_label():
    text = "Routine account. " * 350 + "A controlled experiment tested the result."
    runtime = FakeRuntime(text, fail_window=1)
    result = suggest_frames(
        runtime,
        "ns",
        "frames-2",
        REF,
        document_kind="news",
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy={},
        calibration_policy=policy("frames"),
    )
    assert result["status"] == "abstained"
    assert result["coverage"]["complete"] is False
    assert result["raw_scores"] is None and result["suggested_frames"] == []


def test_frames_abstain_if_model_or_rubric_changes_between_windows():
    class ChangedVersionRuntime(FakeRuntime):
        def run(self, namespace, run_id, task, *, state, questions, **kwargs):
            result = super().run(
                namespace, run_id, task, state=state, questions=questions, **kwargs
            )
            if run_id.endswith(":window:1"):
                result["receipt"]["model_requested"] = "jev-1.14"
            return result

    text = "Routine account. " * 350 + "A controlled experiment tested the result."
    result = suggest_frames(
        ChangedVersionRuntime(text),
        "ns",
        "frames-version-drift",
        REF,
        document_kind="news",
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy={},
        calibration_policy=policy("frames"),
    )
    assert result["status"] == "abstained"
    assert result["coverage"]["complete"] is False
    assert result["raw_scores"] is None
    assert result["suggested_frames"] == []


def test_simultaneous_frames_are_multilabel():
    frozen = policy("frames")
    # A per-label policy is independent: matching two labels retains both.
    frozen_core = {
        key: value for key, value in frozen.items() if key != "policy_sha256"
    }
    frozen_core["thresholds"]["economic"] = frozen_core["thresholds"]["scientific"]
    from src.evaluation.mining_runtime import _hash

    frozen = {**frozen_core, "policy_sha256": _hash(frozen_core)}
    runtime = FakeRuntime(
        "The economic cost was quantified by the experiment in a controlled study."
    )
    result = suggest_frames(
        runtime,
        "ns",
        "frames-multi",
        REF,
        document_kind="paper",
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy={},
        calibration_policy=frozen,
    )
    assert set(result["suggested_frames"]) == {"economic", "scientific"}
    assert result["dominant"] in {"economic", "scientific"}


def test_long_document_abstains_before_remote_call():
    runtime = FakeRuntime("word " * 14000)
    result = suggest_frames(
        runtime,
        "ns",
        "frames-3",
        REF,
        document_kind="book",
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy={},
    )
    assert result["reason"] == "source_exceeds_window_limit"
    assert not runtime.calls


def test_heldout_metrics_keep_abstention_and_baselines():
    frozen = policy("frames")
    rows = [
        {
            **row(
                "test-1",
                "test",
                ["scientific"],
                [0.1, 0.1, 0.1, 0.1, 0.1, 0.9],
                "frames",
            ),
            "baseline_predictions": {
                "legacy_snippet": [],
                "calibrated_full_window": ["scientific"],
            },
        },
        {
            **row("test-2", "test", [], None, "frames", source_type="book"),
            "status": "unavailable",
            "baseline_predictions": {
                "legacy_snippet": [],
                "calibrated_full_window": [],
            },
        },
    ]
    report = evaluate_jev_mining(rows, frozen)
    _validate_evaluation(report)
    assert report["n"] == 2 and report["statuses"]["unavailable"] == 1
    assert report["per_label"]["scientific"]["recall"] == 1
    assert report["dominant_accuracy"] == 1
    assert set(report["by_content_type"]) == {"news", "book"}
    assert set(report["baselines"]) == {"legacy_snippet", "calibrated_full_window"}
    assert report["task_ready"] is False
    with pytest.raises(ValueError, match="unrelated"):
        evaluate_jev_mining([{**rows[0], "group_id": "validation-1"}], frozen)


def test_stance_minority_metrics_and_content_type_results():
    frozen = policy("stance")
    rows = [
        row(
            "critical-1",
            "test",
            ["critical"],
            [0.05, 0.8, 0.1, 0.05],
            "stance",
            "transcript",
        ),
        row(
            "ambiguous-1",
            "test",
            ["ambiguous"],
            [0.05, 0.05, 0.1, 0.8],
            "stance",
            "news",
        ),
        {
            **row("supportive-1", "test", ["supportive"], None, "stance", "book"),
            "status": "abstained",
        },
    ]
    report = evaluate_jev_mining(rows, frozen)
    _validate_evaluation(report)
    assert report["per_label"]["critical"]["precision"] == 1
    assert report["per_label"]["supportive"]["recall"] == 0
    assert set(report["by_content_type"]) == {"transcript", "news", "book"}
    assert report["statuses"]["abstained"] == 1
