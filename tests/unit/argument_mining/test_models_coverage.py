"""
Coverage-focused unit tests for src/argument_mining/models.py.

Targets the model-backed prediction paths (_try_load + _predict_model for both
ClaimDetector and StanceClassifier), fail-closed loading, predict_text empty
results, and the module-level singletons and convenience functions.

transformers is resolved lazily via ``from transformers import pipeline`` inside
_try_load; transformers' lazy loader ignores attribute patching, so a stub
``transformers`` module is injected into sys.modules and a valid config.json
in a temp dir makes _try_load take the model branch.
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest

import src.argument_mining.models as models_mod
from src.argument_mining.models import (
    ClaimDetector,
    ClaimPrediction,
    StanceClassifier,
    StancePrediction,
    get_claim_detector,
    get_stance_classifier,
    predict_claims,
    predict_stance,
)
from services.ingest.common.document_model import Document


def _doc(content: str, source_type: str = "news") -> Document:
    return Document(
        document_id="doc-m",
        source_type=source_type,
        language="en",
        ingested_at=0,
        content=content,
    )


def _stub_transformers(pipe_callable):
    mod = types.ModuleType("transformers")
    mod.pipeline = lambda *args, **kwargs: pipe_callable
    return {"transformers": mod}


def _model_dir_with_config() -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "config.json").write_text("{}")
    return d


def _claim_double() -> ClaimDetector:
    def pipeline(inputs, **_kwargs):
        return [{"label": "LABEL_1", "score": 0.9} for _ in inputs]
    return ClaimDetector(pretrained=(pipeline, "test-claim-model"))


def _stance_double() -> StanceClassifier:
    class NLI:
        prediction_mode = "zero-shot:test-nli-model"

        @staticmethod
        def entailment_scores(pairs):
            return [0.9 if "supportive" in hypothesis else 0.1
                    for _premise, hypothesis in pairs]

    return StanceClassifier(nli=NLI())


_TWO_SENTENCE = (
    "The rate fell to 3.8 percent in March overall. "
    "Markets rose notably higher today across the board."
)


# ---------------------------------------------------------------------------
# ClaimDetector model path
# ---------------------------------------------------------------------------

class TestClaimDetectorModelPath:
    def test_label_1_is_claim_with_raw_confidence(self):
        model_dir = _model_dir_with_config()

        def fake_pipe(inputs, **kwargs):
            return [{"label": "LABEL_1", "score": 0.91} for _ in inputs]

        with mock.patch.dict(sys.modules, _stub_transformers(fake_pipe)):
            cd = ClaimDetector(model_dir=model_dir)
            assert cd._pipeline is not None
            results = cd.predict(_doc(_TWO_SENTENCE))

        assert len(results) == 2
        assert all(r.is_claim for r in results)
        assert all(r.confidence == pytest.approx(0.91) for r in results)
        # sentence indices assigned in order
        assert [r.sentence_idx for r in results] == [0, 1]

    def test_label_0_not_claim_confidence_inverted(self):
        model_dir = _model_dir_with_config()

        def fake_pipe(inputs, **kwargs):
            return [{"label": "LABEL_0", "score": 0.8} for _ in inputs]

        with mock.patch.dict(sys.modules, _stub_transformers(fake_pipe)):
            cd = ClaimDetector(model_dir=model_dir)
            results = cd.predict(_doc(_TWO_SENTENCE))

        assert all(r.is_claim is False for r in results)
        # confidence = 1 - score for the non-claim class
        assert all(r.confidence == pytest.approx(0.2) for r in results)

    def test_predict_text_uses_model_path(self):
        model_dir = _model_dir_with_config()

        def fake_pipe(inputs, **kwargs):
            return [{"label": "LABEL_1", "score": 0.95} for _ in inputs]

        with mock.patch.dict(sys.modules, _stub_transformers(fake_pipe)):
            cd = ClaimDetector(model_dir=model_dir)
            r = cd.predict_text("The company reported quarterly revenue of four billion.")

        assert isinstance(r, ClaimPrediction)
        assert r.is_claim is True
        assert r.confidence == pytest.approx(0.95)


class TestClaimDetectorLoadFailure:
    def test_load_error_fails_closed_when_pretrained_unavailable(self):
        model_dir = _model_dir_with_config()

        def boom(*args, **kwargs):
            raise RuntimeError("weights corrupt")

        mod = types.ModuleType("transformers")
        mod.pipeline = boom
        with mock.patch.dict(sys.modules, {"transformers": mod}), pytest.raises(RuntimeError):
            ClaimDetector(model_dir=model_dir)


# ---------------------------------------------------------------------------
# StanceClassifier model path
# ---------------------------------------------------------------------------

class TestStanceClassifierModelPath:
    def test_label_index_maps_to_stance(self):
        model_dir = _model_dir_with_config()

        # LABEL_2 -> id 2 -> ID2STANCE[2] == "neutral"
        def fake_pipe(inputs, **kwargs):
            # inputs are "topic [SEP] sentence" strings
            assert all("[SEP]" in s for s in inputs)
            return [{"label": "LABEL_2", "score": 0.77} for _ in inputs]

        with mock.patch.dict(sys.modules, _stub_transformers(fake_pipe)):
            sc = StanceClassifier(model_dir=model_dir)
            assert sc._pipeline is not None
            results = sc.predict(_doc(_TWO_SENTENCE), topic="economy")

        assert len(results) == 2
        assert all(r.stance == "neutral" for r in results)
        assert all(r.confidence == pytest.approx(0.77) for r in results)
        assert all(r.topic == "economy" for r in results)

    def test_label_0_maps_to_supportive(self):
        model_dir = _model_dir_with_config()

        def fake_pipe(inputs, **kwargs):
            return [{"label": "LABEL_0", "score": 0.88} for _ in inputs]

        with mock.patch.dict(sys.modules, _stub_transformers(fake_pipe)):
            sc = StanceClassifier(model_dir=model_dir)
            results = sc.predict(_doc(_TWO_SENTENCE), topic="trade")

        assert all(r.stance == "supportive" for r in results)
        assert all(r.confidence == pytest.approx(0.88) for r in results)

    def test_predict_text_uses_model_path(self):
        model_dir = _model_dir_with_config()

        def fake_pipe(inputs, **kwargs):
            return [{"label": "LABEL_1", "score": 0.66} for _ in inputs]

        with mock.patch.dict(sys.modules, _stub_transformers(fake_pipe)):
            sc = StanceClassifier(model_dir=model_dir)
            r = sc.predict_text(
                "The policy has done nothing to address inequality at all.", topic="policy"
            )
        assert isinstance(r, StancePrediction)
        assert r.stance == "critical"  # ID2STANCE[1]
        assert r.confidence == pytest.approx(0.66)


class TestStanceClassifierLoadFailure:
    def test_load_error_fails_closed_when_nli_unavailable(self):
        model_dir = _model_dir_with_config()

        def boom(*args, **kwargs):
            raise RuntimeError("tokenizer missing")

        mod = types.ModuleType("transformers")
        mod.pipeline = boom
        with mock.patch.dict(sys.modules, {"transformers": mod}), pytest.raises(RuntimeError):
            StanceClassifier(model_dir=model_dir)


# ---------------------------------------------------------------------------
# predict_text empty-result fallbacks (no sentences >= 20 chars)
# ---------------------------------------------------------------------------

class TestPredictTextEmptyFallback:
    def test_claim_predict_text_short_input_fallback(self):
        cd = _claim_double()
        # "short" is < 20 chars -> no sentences -> constructed fallback
        r = cd.predict_text("short")
        assert r.is_claim is False
        assert r.confidence == 0.5
        assert r.text == "short"
        assert r.sentence_idx == 0

    def test_stance_predict_text_short_input_fallback(self):
        sc = _stance_double()
        r = sc.predict_text("short", topic="topic-x")
        assert r.stance == "neutral"
        assert r.confidence == 0.5
        assert r.topic == "topic-x"
        assert r.text == "short"


# ---------------------------------------------------------------------------
# Module-level singletons / convenience
# ---------------------------------------------------------------------------

class TestModuleHelpers:
    def test_get_claim_detector_singleton(self):
        models_mod._claim_detector = _claim_double()
        try:
            first = get_claim_detector()
            second = get_claim_detector()
            assert first is second
            assert isinstance(first, ClaimDetector)
        finally:
            models_mod._claim_detector = None

    def test_get_stance_classifier_singleton(self):
        models_mod._stance_classifier = _stance_double()
        try:
            first = get_stance_classifier()
            second = get_stance_classifier()
            assert first is second
            assert isinstance(first, StanceClassifier)
        finally:
            models_mod._stance_classifier = None

    def test_predict_claims_convenience(self):
        models_mod._claim_detector = _claim_double()
        try:
            results = predict_claims(
                _doc("The central bank raised interest rates by 25 basis points to 5.25%.")
            )
            assert len(results) >= 1
            assert all(isinstance(r, ClaimPrediction) for r in results)
        finally:
            models_mod._claim_detector = None

    def test_predict_stance_convenience(self):
        models_mod._stance_classifier = _stance_double()
        try:
            results = predict_stance(
                _doc("This landmark reform delivers vital protections for workers."),
                topic="labor",
            )
            assert len(results) >= 1
            assert all(isinstance(r, StancePrediction) for r in results)
            assert all(r.topic == "labor" for r in results)
        finally:
            models_mod._stance_classifier = None
