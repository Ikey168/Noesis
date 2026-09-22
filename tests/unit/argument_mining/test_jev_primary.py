from __future__ import annotations

from services.ingest.common.document_model import Document
from src.argument_mining import frames as frames_mod
from src.argument_mining import models as models_mod
from src.argument_mining.frames import (
    FRAME_LABELS,
    FramePrediction,
    JevPrimaryFrameClassifier,
    get_frame_classifier,
)
from src.argument_mining.models import (
    ClaimPrediction,
    JevPrimaryClaimDetector,
    JevPrimaryStanceClassifier,
    StancePrediction,
    get_claim_detector,
    get_stance_classifier,
)
from src.integrations.typesafe_jev import JevConfig


def _doc(text: str) -> Document:
    return Document(
        document_id="d1",
        source_type="news",
        language="en",
        ingested_at=1,
        content=text,
    )


class FakeJev:
    def __init__(self, answers):
        self.config = JevConfig(api_key="test")
        self.last_model = "jev-1.13.0"
        self.answers = answers
        self.calls = []

    def system_one(self, *, state, questions):
        self.calls.append((state, questions))
        return {"model": self.last_model, "answers": self.answers}


def test_jev_is_primary_for_claim_detection_without_loading_fallback():
    jev = FakeJev(
        {
            "claim_0": {"type": "noul", "noul": 0.97},
            "claim_1": {"type": "noul", "noul": 0.08},
        }
    )

    def forbidden_fallback():
        raise AssertionError("fallback should stay lazy")

    detector = JevPrimaryClaimDetector(
        client=jev,
        fallback_factory=forbidden_fallback,
    )
    result = detector.predict(_doc("The launch succeeded. Please investigate why."))

    assert [item.is_claim for item in result] == [True, False]
    assert detector.prediction_mode == "jev:jev-1.13.0"
    assert len(jev.calls) == 1
    assert len(jev.calls[0][1]) == 2


def test_uncertain_claim_decision_uses_dedicated_fallback():
    jev = FakeJev({"claim_0": {"type": "noul", "noul": 0.55}})

    class Fallback:
        prediction_mode = "pretrained:fallback"

        def predict(self, document):
            return [ClaimPrediction(document.content, 0, True, 0.91)]

    detector = JevPrimaryClaimDetector(client=jev, fallback_factory=Fallback)
    result = detector.predict(_doc("The launch succeeded."))

    assert result[0].confidence == 0.91
    assert detector.prediction_mode == "pretrained:fallback"


def test_jev_is_primary_for_stance_when_choice_is_confident():
    jev = FakeJev(
        {
            "stance_0": {
                "type": "choice",
                "choice": "supportive",
                "probabilities": {
                    "supportive": 0.9,
                    "critical": 0.02,
                    "neutral": 0.06,
                    "ambiguous": 0.02,
                },
                "confidence": 0.86,
            }
        }
    )

    def forbidden_fallback():
        raise AssertionError("fallback should stay lazy")

    classifier = JevPrimaryStanceClassifier(
        client=jev,
        fallback_factory=forbidden_fallback,
    )
    result = classifier.predict(_doc("The reform is a major success."), "reform")

    assert result == [
        StancePrediction(
            "The reform is a major success.",
            0,
            "reform",
            "supportive",
            0.86,
        )
    ]
    assert classifier.prediction_mode == "jev:jev-1.13.0"


def test_low_confidence_stance_uses_dedicated_fallback():
    jev = FakeJev(
        {
            "stance_0": {
                "type": "choice",
                "choice": "neutral",
                "probabilities": {"neutral": 0.4},
                "confidence": 0.2,
            }
        }
    )

    class Fallback:
        prediction_mode = "zero-shot:fallback"

        def predict(self, document, topic):
            return [StancePrediction(document.content, 0, topic, "critical", 0.8)]

    classifier = JevPrimaryStanceClassifier(client=jev, fallback_factory=Fallback)
    result = classifier.predict(
        _doc("The reform failed to deliver the promised worker protections."),
        "reform",
    )

    assert result[0].stance == "critical"
    assert classifier.prediction_mode == "zero-shot:fallback"


def test_public_getters_select_jev_when_key_is_configured(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    models_mod._claim_detector = None
    models_mod._stance_classifier = None
    frames_mod._frame_classifier = None
    try:
        assert isinstance(get_claim_detector(), JevPrimaryClaimDetector)
        assert isinstance(get_stance_classifier(), JevPrimaryStanceClassifier)
        assert isinstance(get_frame_classifier(), JevPrimaryFrameClassifier)
    finally:
        models_mod._claim_detector = None
        models_mod._stance_classifier = None
        frames_mod._frame_classifier = None


def test_frame_classifier_falls_back_only_for_threshold_near_frames():
    answers = {}
    for frame in FRAME_LABELS:
        probability = 0.9 if frame == "scientific" else 0.05
        if frame == "economic":
            probability = 0.46
        answers[f"frame_{frame}"] = {"type": "noul", "noul": probability}
    jev = FakeJev(answers)

    class Fallback:
        prediction_mode = "zero-shot:fallback"

        def predict(self, document):
            scores = {frame: 0.0 for frame in FRAME_LABELS}
            scores["economic"] = 0.72
            return FramePrediction(
                document_id=document.document_id,
                source_type=document.source_type,
                frames=scores,
                dominant="economic",
            )

    classifier = JevPrimaryFrameClassifier(client=jev, fallback_factory=Fallback)
    result = classifier.predict(_doc("Researchers reported a new clinical result."))

    assert result.frames["scientific"] == 0.9
    assert result.frames["economic"] == 0.72
    assert result.dominant == "scientific"
    assert result.prediction_mode == "jev:jev-1.13.0+fallback:zero-shot:fallback"
