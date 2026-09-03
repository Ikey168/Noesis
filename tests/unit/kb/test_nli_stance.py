"""Unit tests for the zero-shot NLI stance backend (#954)."""

from src.argument_mining.models import StanceClassifier
from src.kb.nli import ENTAILMENT, NEUTRAL, NLIResult


class ScriptedNLI:
    """NLI double: entails the template naming the scripted stance."""

    name = "nli:fake-nli"
    prediction_mode = "zero-shot:fake-nli"
    model_version = "fake-nli@r1"

    def __init__(self):
        self.calls = 0

    def classify(self, premise, hypothesis):
        self.calls += 1
        p = premise.lower()
        if "praised" in p and "supportive" in hypothesis:
            return NLIResult(ENTAILMENT, 0.9, self.prediction_mode)
        if "condemned" in p and "critical" in hypothesis:
            return NLIResult(ENTAILMENT, 0.88, self.prediction_mode)
        if "reported" in p and "neutrally" in hypothesis:
            return NLIResult(ENTAILMENT, 0.7, self.prediction_mode)
        return NLIResult(NEUTRAL, 0.6, self.prediction_mode)


class TestNLIStance:
    def test_supportive_and_critical_via_templates(self):
        classifier = StanceClassifier(nli=ScriptedNLI())
        assert classifier.prediction_mode == "zero-shot:fake-nli"

        supportive = classifier.predict_text(
            "Lawmakers praised the stablecoin framework.", topic="stablecoin regulation"
        )
        assert supportive.stance == "supportive"
        assert 0.0 < supportive.confidence <= 1.0

        critical = classifier.predict_text(
            "Economists condemned the stablecoin framework.", topic="stablecoin regulation"
        )
        assert critical.stance == "critical"

    def test_floor_defaults_to_neutral(self):
        classifier = StanceClassifier(nli=ScriptedNLI())
        prediction = classifier.predict_text(
            "The committee met on Tuesday.", topic="stablecoin regulation"
        )
        assert prediction.stance == "neutral"
        assert prediction.confidence == 0.5

    def test_results_cached_per_sentence_topic(self):
        nli = ScriptedNLI()
        classifier = StanceClassifier(nli=nli)
        classifier.predict_text("Lawmakers praised the framework.", topic="t")
        first_calls = nli.calls
        classifier.predict_text("Lawmakers praised the framework.", topic="t")
        assert nli.calls == first_calls  # cache hit, no new NLI calls

    def test_explicit_opt_out_stays_heuristic(self, monkeypatch):
        monkeypatch.setenv("NOESIS_STANCE_BACKEND", "heuristic")
        classifier = StanceClassifier()
        assert classifier.prediction_mode == "heuristic"
        prediction = classifier.predict_text("Anything at all.", topic="t")
        assert prediction.stance in {"supportive", "critical", "neutral", "ambiguous"}

    def test_env_opt_in_survives_missing_stack(self, monkeypatch):
        # With the env set but no transformers/model available, the wrapper
        # must fall back to heuristic instead of raising.
        monkeypatch.setenv("NOESIS_STANCE_BACKEND", "nli")
        monkeypatch.setenv("NOESIS_NLI_MODEL", "definitely/not-a-real-model")
        classifier = StanceClassifier()
        assert classifier.prediction_mode in ("heuristic",) or classifier._nli is None
