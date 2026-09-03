"""Unit tests for the zero-shot NLI frame backend (#955)."""

import pytest

from src.argument_mining.frames import FrameClassifier
from src.kb.nli import ENTAILMENT, NEUTRAL, NLIResult


class ScriptedNLI:
    """Entails the hypotheses whose frame word appears in the premise."""

    name = "nli:fake-nli"
    prediction_mode = "zero-shot:fake-nli"
    model_version = "fake-nli@r1"

    SIGNALS = {
        "political": ("parliament", 0.6),
        "economic": ("tariff", 0.8),
        "humanitarian": ("refugees", 0.4),
    }

    def classify(self, premise, hypothesis):
        p = premise.lower()
        for frame, (token, score) in self.SIGNALS.items():
            if token in p and f"{frame} issue" in hypothesis:
                return NLIResult(ENTAILMENT, score, self.prediction_mode)
        return NLIResult(NEUTRAL, 0.6, self.prediction_mode)


class TestNLIFrames:
    def test_multi_label_scores_and_dominant(self):
        classifier = FrameClassifier(nli=ScriptedNLI())
        assert classifier.prediction_mode == "zero-shot:fake-nli"

        prediction = classifier.predict_text(
            "Parliament debated the tariff bill as refugees arrived.",
        )
        # economic (0.8) and political (0.6) clear their thresholds;
        # humanitarian (0.4) clears its *permissive* 0.35 threshold.
        assert prediction.frames["economic"] == 0.8
        assert prediction.frames["political"] == 0.6
        assert prediction.frames["humanitarian"] == 0.4
        assert prediction.frames["scientific"] == 0.0
        assert prediction.dominant == "economic"

    def test_permissive_thresholds_recover_starved_frames(self):
        classifier = FrameClassifier(nli=ScriptedNLI())
        prediction = classifier.predict_text("Refugees crossed the border.")
        # 0.4 would fail the default 0.45 threshold; the per-label 0.35
        # threshold for humanitarian keeps it on.
        assert prediction.frames["humanitarian"] == 0.4
        assert prediction.dominant == "humanitarian"

    def test_nothing_clears_thresholds_means_other(self):
        classifier = FrameClassifier(nli=ScriptedNLI())
        prediction = classifier.predict_text("A quiet afternoon by the lake.")
        assert prediction.dominant == "other"

    def test_removed_backend_setting_is_rejected(self, monkeypatch):
        monkeypatch.setenv("NOESIS_FRAMES_BACKEND", "heuristic")
        with pytest.raises(ValueError, match="has been removed"):
            FrameClassifier()

    def test_missing_nli_weights_fail_closed(self, monkeypatch):
        monkeypatch.setenv("NOESIS_FRAMES_BACKEND", "nli")
        monkeypatch.setenv("NOESIS_NLI_MODEL", "definitely/not-a-real-model")
        with pytest.raises(RuntimeError, match="make models"):
            FrameClassifier()
