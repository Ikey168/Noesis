"""Regression contracts for calibration and model execution; no human labels asserted."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.evaluation.mining_runtime import (
    CalibratedMiningBackend,
    MultilingualNLI,
    _hash,
    apply_policy,
    evaluate_policy,
    fit_policy,
)
from src.evaluation.runtime_errors import BackendError


def rows(split, task="stance"):
    return [
        {
            "id": split + str(i),
            "group_id": split + "-group-" + str(i),
            "split": split,
            "scores": [0.9, 0.1] if i % 2 == 0 else [0.1, 0.9],
            "labels": ["a" if i % 2 == 0 else "b"],
            "label_origin": "fixture",
            "source": "fixture-source",
            "domain": "research",
            "language": "de",
        }
        for i in range(4)
    ]


@pytest.mark.parametrize("task", ["stance", "frames", "nli"])
def test_calibration_selects_only_validation_and_keeps_test_unseen(task):
    policy = fit_policy(
        rows("validation"),
        ["a", "b"],
        task=task,
        model_version="fixture-v1",
        template_version="template-v1",
    )
    report = evaluate_policy(rows("test"), policy)
    assert report["metrics"]["macro_f1"] == 1
    assert report["label_origins"] == ["fixture"] and not report["task_ready"]
    assert report["metrics"]["per_class_calibration"]
    test = rows("test")
    test[0]["group_id"] = policy["validation_groups"][0]
    with pytest.raises(ValueError, match="leakage"):
        evaluate_policy(test, policy)
    with pytest.raises(ValueError, match="validation"):
        fit_policy(
            rows("test"),
            ["a", "b"],
            task=task,
            model_version="fixture-v1",
            template_version="template-v1",
        )
    tampered = deepcopy(policy)
    tampered["temperature"] = 9
    with pytest.raises(ValueError, match="hash"):
        evaluate_policy(rows("test"), tampered)


def test_calibrated_mining_retains_full_evidence_and_checks_model():
    templates = {"a": "This supports {topic}.", "b": "This opposes {topic}."}
    policy = fit_policy(
        rows("validation"),
        list(templates),
        task="stance",
        model_version="fixture-v1",
        template_version=_hash(templates),
    )

    class NLI:
        model_version = "fixture-v1"

        def classify_evidence(self, text, hypothesis):
            assert text.endswith("TAIL") and len(text) > 1500
            return {
                "label": "entailment",
                "confidence": 0.9 if "supports" in hypothesis else 0.1,
                "coverage_complete": True,
                "windows": [{"start": 0, "end": len(text)}],
            }

    backend = CalibratedMiningBackend(NLI(), policy, templates)
    result = backend.predict("Evidence " * 300 + "TAIL", topic="policy")
    assert (
        result["labels"] == ["a"]
        and result["evidence"][0]["assessment"]["coverage_complete"]
    )
    NLI.model_version = "other-model"
    with pytest.raises(ValueError, match="changed"):
        CalibratedMiningBackend(NLI(), policy, templates)


def test_low_confidence_remains_unsupported_not_neutral():
    policy = fit_policy(
        rows("validation"),
        ["a", "b"],
        task="stance",
        model_version="fixture-v1",
        template_version="v1",
    )
    policy["threshold"] = 0.9
    assert apply_policy([0.5, 0.5], policy)["status"] == "unsupported"


def test_mdeberta_runs_premise_first_and_verifies_three_label_mapping():
    torch = pytest.importorskip("torch")

    class Tokenizer:
        def __call__(self, premises, hypotheses, **kwargs):
            assert premises == ["Beleg"] and hypotheses == ["Behauptung"]
            assert kwargs["truncation"] is False
            return {"input_ids": torch.tensor([[1, 2, 3]])}

    class Model:
        config = SimpleNamespace(
            id2label={0: "entailment", 1: "neutral", 2: "contradiction"}
        )
        device = "cpu"

        def __call__(self, **kwargs):
            return SimpleNamespace(logits=torch.tensor([[0.0, 0.0, 2.0]]))

    backend = MultilingualNLI(model=Model(), tokenizer=Tokenizer())
    result = backend.classify("Beleg", "Behauptung")
    assert result.label == "contradiction"
    assert sum(
        backend.probabilities([("Beleg", "Behauptung")])[0].values()
    ) == pytest.approx(1)
    Model.config = SimpleNamespace(id2label={0: "LABEL_0", 1: "LABEL_1", 2: "LABEL_2"})
    with pytest.raises(BackendError, match="explicit"):
        MultilingualNLI(model=Model(), tokenizer=Tokenizer())


def test_existing_stance_frames_and_chunker_can_use_optional_runners():
    from services.ingest.common.document_model import Document
    from services.rag.chunking import ChunkConfig, TextChunker
    from src.argument_mining.frames import FrameClassifier
    from src.argument_mining.models import StanceClassifier

    class Runner:
        def __init__(self):
            self.policy = {"policy_sha256": "fixture-policy"}

        def predict(self, text, **kwargs):
            return {"labels": [], "scores": {}}

    doc = Document(
        document_id="s",
        source_type="news",
        language="de",
        ingested_at=0,
        content="Ein vollständiger deutscher Beleg.",
    )
    assert (
        StanceClassifier(calibration=Runner()).predict(doc, "topic")[0].stance
        == "unsupported"
    )
    assert FrameClassifier(calibration=Runner()).predict(doc).dominant == "unsupported"

    class Segmenter:
        def segment(self, text):
            return [{"text": text, "start": 0, "end": len(text)}]

    chunker = TextChunker(ChunkConfig(min_chunk_chars=0), segmenter=Segmenter())
    assert chunker._sentence_spans("Dr. Müller.") == [(0, 11)]
