from __future__ import annotations

import pytest

from src.argument_mining.jev_nlp_adapters import (
    suggest_attribution,
    suggest_checkworthiness,
    suggest_claim_presence,
    suggest_sentiment,
)
from src.evaluation.jev_nlp import (
    evaluate_categorical_nlp,
    evaluate_checkworthiness,
    evaluate_claim_presence,
    fit_categorical_acceptance_policy,
    fit_claim_presence_policy,
)


REF = {"document_id": "doc", "revision_id": "rev"}


class FakeRuntime:
    def __init__(self, text, answers=None, *, unavailable=False):
        self.text, self.answers, self.unavailable, self.calls = (
            text,
            answers or {},
            unavailable,
            [],
        )

    def capture_sources(self, namespace, principal_id, refs, scopes):
        assert refs == [REF]
        return ([{"kind": "document_revision", **REF}], [{"content": self.text}])

    def run(
        self, namespace, run_id, task, *, state, source_slices, questions, **kwargs
    ):
        self.calls.append(
            {
                "task": task,
                "state": state,
                "source_slices": source_slices,
                "questions": questions,
            }
        )
        if self.unavailable:
            return {
                "status": "unavailable",
                "rollout_mode": "manual",
                "receipt": None,
                "source_binding": [REF],
            }
        answers = {}
        for key, question in questions.items():
            value = self.answers.get(key)
            if question["type"] == "noul":
                answers[key] = {
                    "kind": "noul",
                    "status": "answered",
                    "value": 0.2 if value is None else value,
                }
            elif question["type"] == "score":
                answers[key] = {
                    "kind": "score",
                    "status": "answered",
                    "value": 2.0 if value is None else value,
                }
            else:
                choice = value or next(iter(question["criteria"]))
                probabilities = {
                    label: (
                        0.8
                        if label == choice
                        else 0.2 / (len(question["criteria"]) - 1)
                    )
                    for label in question["criteria"]
                }
                answers[key] = {
                    "kind": "choice",
                    "status": "answered",
                    "value": choice,
                    "probabilities": probabilities,
                    "selected_probability": 0.8,
                    "vendor_confidence": 0.6,
                }
        return {
            "status": "completed",
            "rollout_mode": "manual",
            "receipt": {
                "answers": answers,
                "model_requested": "jev-1.13",
                "rubric_id": "v1",
            },
            "source_binding": [REF],
        }


def eval_row(case_id, split, truth, p, source_type, case_kind):
    return {
        "id": case_id,
        "group_id": case_id,
        "split": split,
        "truth": truth,
        "p_claim": p,
        "status": "completed" if p is not None else "unavailable",
        "source_type": source_type,
        "language": "en",
        "case_kind": case_kind,
        "label_origin": "fixture",
    }


def categorical_policy(task):
    rows = [
        {
            **eval_row("validation-cat", "validation", task, None, "news", "quoted"),
            "selected_probability": 0.8,
            "correct": True,
        }
    ]
    return fit_categorical_acceptance_policy(
        rows, task=task, model_version="jev-1.13", rubric_version="v1"
    )


def test_claim_presence_negative_probability_and_exact_sentence_context():
    text = "The speaker asked whether it had happened. The agency reported a five percent rise."
    runtime = FakeRuntime(text, {"factual_claim": 0.2})
    policy = fit_claim_presence_policy(
        [
            eval_row("v1", "validation", "nonclaim", 0.2, "news", "quoted"),
            eval_row("v2", "validation", "claim", 0.8, "transcript", "long"),
        ],
        model_version="jev-1.13",
        rubric_version="v1",
    )
    result = suggest_claim_presence(
        runtime,
        "ns",
        "claim-1",
        REF,
        sentence_index=0,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
        calibration_policy=policy,
    )
    assert result["p_claim"] == 0.2 and result["p_nonclaim"] == 0.8
    assert result["suggested_class"] == "nonclaim"
    assert result["selected_class_confidence"] == 0.8
    assert result["text"] == text[result["span"]["start"] : result["span"]["end"]]
    assert runtime.calls[0]["source_slices"] == [result["context_span"]]
    assert result["accepted"] is False


def test_checkworthiness_never_changes_claim_presence_or_truth():
    text = "The report states that emissions fell by five percent."
    runtime = FakeRuntime(text, {"impact": 2, "testability": 3})
    result = suggest_checkworthiness(
        runtime,
        "ns",
        "priority-1",
        REF,
        {"start": 0, "end": len(text), "quote": text},
        claim_id="claim-1",
        topic="emissions",
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert result["claim_present"] is True and result["truth_status"] is None
    assert result["priority_score"] == pytest.approx((0.6 * 2 + 0.4 * 3) / 3)
    assert result["scheduler_mutated"] is False
    runtime.unavailable = True
    missing = suggest_checkworthiness(
        runtime,
        "ns",
        "priority-2",
        REF,
        {"start": 0, "end": len(text)},
        claim_id="claim-1",
        topic="emissions",
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert missing["status"] == "abstained" and missing["claim_present"] is True


def test_sentiment_maps_choice_without_writing_trends_or_trust():
    text = 'The article says the launch was "great" but the service was costly.'
    runtime = FakeRuntime(text, {"sentiment": "mixed"})
    result = suggest_sentiment(
        runtime,
        "ns",
        "sentiment-1",
        REF,
        {"start": 0, "end": len(text)},
        target="the service",
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
        calibration_policy=categorical_policy("sentiment"),
    )
    assert result["suggested_sentiment"] == {
        "label": "MIXED",
        "score": 0.8,
        "text": text,
    }
    assert result["raw_distribution"]["mixed"] == 0.8
    assert result["trend_mutated"] is False and result["source_trust_inferred"] is False
    assert runtime.calls[0]["state"]["target"] == "the service"


def test_attribution_restricts_to_extracted_candidates_and_preserves_metadata():
    text = 'Alex said, "The vote passed." Pat replied, "It did not."'
    candidates = [
        {"id": "alex", "name": "Alex", "role": "speaker"},
        {"id": "pat", "name": "Pat", "role": "speaker"},
    ]
    runtime = FakeRuntime(text, {"speaker": "uncertain"})
    result = suggest_attribution(
        runtime,
        "ns",
        "speaker-1",
        REF,
        {"start": 0, "end": len(text)},
        candidates,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
        calibration_policy=categorical_policy("attribution"),
        authors=["Reporter"],
        byline="By Reporter",
    )
    assert result["status"] == "abstained" and result["suggested_actor"] is None
    assert result["raw_choice"] == "uncertain"
    assert result["authors"] == ["Reporter"] and result["byline"] == "By Reporter"
    assert result["source_metadata_mutated"] is False
    runtime.answers["speaker"] = "pat"
    selected = suggest_attribution(
        runtime,
        "ns",
        "speaker-2",
        REF,
        {"start": 0, "end": len(text)},
        candidates,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
        calibration_policy=categorical_policy("attribution"),
    )
    assert selected["suggested_actor"]["id"] == "pat"
    runtime.answers["speaker"] = "none"
    no_match = suggest_attribution(
        runtime,
        "ns",
        "speaker-2-none",
        REF,
        {"start": 0, "end": len(text)},
        candidates,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
        calibration_policy=categorical_policy("attribution"),
    )
    assert no_match["status"] == "suggested"
    assert no_match["suggested_choice"] == "none"
    assert no_match["suggested_actor"] is None
    runtime.answers["speaker"] = "uncertain"
    uncertain = suggest_attribution(
        runtime,
        "ns",
        "speaker-2-uncertain",
        REF,
        {"start": 0, "end": len(text)},
        candidates,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
        calibration_policy=categorical_policy("attribution"),
    )
    assert uncertain["status"] == "abstained"
    assert uncertain["raw_choice"] == "uncertain"
    assert uncertain["suggested_choice"] is None
    with pytest.raises(ValueError, match="candidate"):
        suggest_attribution(
            runtime,
            "ns",
            "speaker-3",
            REF,
            {"start": 0, "end": len(text)},
            [{"id": "none", "name": "Unknown"}],
            principal_id="alice",
            scopes=set(),
            allow_remote=True,
            policy={},
        )


def test_offline_evaluators_are_stratified_and_not_task_ready():
    frozen = fit_claim_presence_policy(
        [
            eval_row("v1", "validation", "claim", 0.8, "news", "long"),
            eval_row("v2", "validation", "nonclaim", 0.2, "blog", "quoted"),
        ],
        model_version="jev-1.13",
        rubric_version="v1",
    )
    cases = [
        eval_row(
            f"t{i}",
            "test",
            "claim" if i % 2 else "nonclaim",
            0.8 if i % 2 else 0.2,
            source,
            kind,
        )
        for i, (source, kind) in enumerate(
            zip(
                ("news", "blog", "paper", "transcript", "book", "note"),
                ("long", "quoted", "negation", "transcript", "short", "question"),
                strict=True,
            )
        )
    ]
    claim = evaluate_claim_presence(cases, frozen)
    assert set(claim["strata"]["source_type"]) == {
        "news",
        "blog",
        "paper",
        "transcript",
        "book",
        "note",
    }
    assert claim["per_class"]["claim"]["recall"] == 1
    assert claim["task_ready"] is False
    priorities = evaluate_checkworthiness(
        [
            {
                **eval_row("p1", "test", "claim", None, "news", "high"),
                "gold_priority": 3,
                "jev_score": 0.9,
                "baseline_score": 0.2,
            },
            {
                **eval_row("p2", "test", "claim", None, "news", "low"),
                "gold_priority": 0,
                "jev_score": 0.1,
                "baseline_score": 0.8,
            },
        ],
        budget=1,
    )
    assert priorities["jev"]["high_priority_precision"] == 1
    assert priorities["baseline"]["high_priority_precision"] == 0
    sentiment = evaluate_categorical_nlp(
        [
            {
                **eval_row("s1", "test", "mixed", None, "news", "sarcastic"),
                "prediction": "mixed",
            },
            {
                **eval_row("s2", "test", "neutral", None, "blog", "quoted"),
                "prediction": None,
            },
        ],
        task="sentiment",
    )
    assert sentiment["strata"]["case_kind"]["sarcastic"]["precision"] == 1
    attribution = evaluate_categorical_nlp(
        [
            {
                **eval_row("a1", "test", "alex", None, "transcript", "nested_quote"),
                "candidate_ids": ["alex", "pat"],
                "prediction": "alex",
            },
        ],
        task="attribution",
    )
    assert attribution["overall"]["precision"] == 1
    assert attribution["task_ready"] is False


def test_named_local_surfaces_expose_opt_in_suggestion_paths():
    from src.argument_mining.models import suggest_claim_with_jev
    from src.argument_mining.factcheck_scheduler import suggest_checkworthiness_with_jev
    from src.argument_mining.attribution import suggest_speaker_with_jev
    from src.nlp.sentiment_pipeline import suggest_sentiment_with_jev

    text = "The agency reported a five percent rise in emissions."
    runtime = FakeRuntime(
        text,
        {
            "factual_claim": 0.8,
            "impact": 2,
            "testability": 2,
            "sentiment": "neutral",
            "speaker": "none",
        },
    )
    common = {
        "principal_id": "alice",
        "scopes": set(),
        "allow_remote": True,
        "policy": {},
    }
    claim = suggest_claim_with_jev(
        runtime, "ns", "surface-claim", REF, sentence_index=0, **common
    )
    assert claim["p_claim"] == 0.8 and claim["accepted"] is False
    priority = suggest_checkworthiness_with_jev(
        runtime,
        "ns",
        "surface-priority",
        REF,
        {"start": 0, "end": len(text)},
        claim_id="claim-1",
        topic="emissions",
        **common,
    )
    assert priority["claim_present"] is True and priority["scheduler_mutated"] is False
    sentiment = suggest_sentiment_with_jev(
        runtime,
        "ns",
        "surface-sentiment",
        REF,
        {"start": 0, "end": len(text)},
        target="emissions",
        **common,
    )
    assert sentiment["suggested_sentiment"] is None
    speaker = suggest_speaker_with_jev(
        runtime,
        "ns",
        "surface-speaker",
        REF,
        {"start": 0, "end": len(text)},
        [{"id": "agency", "name": "Agency", "role": "speaker"}],
        **common,
    )
    assert speaker["suggested_actor"] is None and speaker["raw_choice"] == "none"


def test_malformed_model_values_are_abstained_instead_of_becoming_suggestions():
    class MalformedRuntime(FakeRuntime):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            task = args[2]
            answers = result["receipt"]["answers"]
            if task.endswith("claim_detection-v1"):
                answers["factual_claim"]["value"] = float("nan")
            elif task.endswith("checkworthiness-v1"):
                answers["impact"]["value"] = float("inf")
            elif task.endswith("sentiment-v1"):
                answers["sentiment"]["probabilities"]["mixed"] = 5
            elif task.endswith("attribution-v1"):
                answers["speaker"]["value"] = "invented-actor"
            return result

    text = "The agency said emissions changed by five percent."
    candidates = [{"id": "agency", "name": "Agency", "role": "institution"}]
    common = {
        "principal_id": "alice",
        "scopes": set(),
        "allow_remote": True,
        "policy": {},
    }
    claim = suggest_claim_presence(
        MalformedRuntime(text), "ns", "bad-claim", REF, sentence_index=0, **common
    )
    priority = suggest_checkworthiness(
        MalformedRuntime(text), "ns", "bad-priority", REF,
        {"start": 0, "end": len(text)}, claim_id="claim-1", topic="emissions",
        **common,
    )
    sentiment = suggest_sentiment(
        MalformedRuntime(text), "ns", "bad-sentiment", REF,
        {"start": 0, "end": len(text)}, target="emissions", **common,
    )
    attribution = suggest_attribution(
        MalformedRuntime(text), "ns", "bad-attribution", REF,
        {"start": 0, "end": len(text)}, candidates, **common,
    )
    assert claim["status"] == "abstained" and claim["p_claim"] is None
    assert priority["status"] == "abstained" and priority["priority_score"] is None
    assert sentiment["status"] == "abstained" and sentiment["raw_distribution"] is None
    assert attribution["status"] == "abstained" and attribution["raw_choice"] is None
