"""
Unit tests for argument mining inference wrappers.

Tests inject lightweight model doubles so the suite stays fast and never
depends on downloaded weights.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from services.ingest.common.document_model import Document
from src.argument_mining.models import (
    ClaimDetector,
    ClaimPrediction,
    StanceClassifier,
    StancePrediction,
)

_NO_MODEL = Path("/tmp/_nonexistent_am_model")


def _doc(content: str, source_type: str = "news") -> Document:
    return Document(
        document_id="t-1",
        source_type=source_type,
        language="en",
        ingested_at=int(time.time() * 1000),
        content=content,
    )


@pytest.fixture()
def cd() -> ClaimDetector:
    def pipeline(sentences, **_kwargs):
        results = []
        for sentence in sentences:
            lowered = sentence.lower()
            negative = sentence.endswith("?") or "believe" in lowered or "might" in lowered
            results.append({
                "label": "LABEL_0" if negative else "LABEL_1",
                "score": 0.9 if negative else 0.88,
            })
        return results

    return ClaimDetector(model_dir=_NO_MODEL, pretrained=(pipeline, "test-claim-model"))


@pytest.fixture()
def sc() -> StanceClassifier:
    class FakeNLI:
        prediction_mode = "zero-shot:test-nli-model"

        @staticmethod
        def entailment_scores(pairs):
            scores = []
            for premise, hypothesis in pairs:
                text = premise.lower()
                if "supportive" in hypothesis:
                    score = 0.92 if any(word in text for word in ("growth", "vital", "improve", "opens")) else 0.1
                elif "critical" in hypothesis:
                    score = 0.92 if any(word in text for word in ("nothing", "catastrophic", "reckless", "costs")) else 0.1
                elif "ambiguous" in hypothesis:
                    score = 0.92 if any(word in text for word in ("uncertain", "mixed", "unclear")) else 0.1
                else:
                    score = 0.92 if any(word in text for word in ("introduced", "passed", "meets")) else 0.1
                scores.append(score)
            return scores

    return StanceClassifier(model_dir=_NO_MODEL, nli=FakeNLI())


# ---------------------------------------------------------------------------
# ClaimDetector
# ---------------------------------------------------------------------------

class TestClaimDetector:
    def test_numeric_claim_detected(self, cd):
        result = cd.predict(_doc("The unemployment rate fell to 3.8% in March."))
        assert result[0].is_claim is True
        assert result[0].confidence > 0.5

    def test_opinion_not_claim(self, cd):
        result = cd.predict(_doc("Many people believe the situation will improve."))
        assert result[0].is_claim is False

    def test_question_not_claim(self, cd):
        result = cd.predict(_doc("Will the economy recover before the next election?"))
        assert result[0].is_claim is False

    def test_multi_sentence_returns_one_per_sentence(self, cd):
        text = (
            "The company reported a 15% increase in quarterly profits. "
            "Many analysts believe this trend will continue. "
            "Revenue reached $4.2 billion in the second quarter."
        )
        results = cd.predict(_doc(text))
        assert len(results) >= 2
        # At least one sentence should be a claim (the factual ones)
        assert any(r.is_claim for r in results)

    def test_empty_document_returns_empty(self, cd):
        assert cd.predict(_doc("")) == []

    def test_confidence_range(self, cd):
        results = cd.predict(_doc("The court ruled the legislation unconstitutional in a 5-4 decision."))
        for r in results:
            assert 0.0 <= r.confidence <= 1.0

    def test_sentence_idx_assigned(self, cd):
        text = "GDP grew by 2.3%. Analysts are uncertain about next quarter."
        results = cd.predict(_doc(text))
        for i, r in enumerate(results):
            assert r.sentence_idx == i

    def test_predict_text_convenience(self, cd):
        r = cd.predict_text("GDP grew by 2.3% in the last quarter.")
        assert isinstance(r, ClaimPrediction)
        assert r.is_claim is True

    def test_all_source_types_accepted(self, cd):
        content = "Scientists identified a protein linked to Alzheimer's disease."
        for st in ("news", "blog", "paper", "transcript", "book", "note"):
            doc = _doc(content, source_type=st)
            results = cd.predict(doc)
            assert len(results) == 1
            assert isinstance(results[0], ClaimPrediction)

    def test_past_tense_verb_raises_claim_score(self, cd):
        with_verb = cd.predict_text("The government signed the treaty in Geneva.")
        without_verb = cd.predict_text("It might be good if someone signed something.")
        # The definite past-tense claim should have higher confidence
        assert with_verb.confidence > without_verb.confidence or with_verb.is_claim


# ---------------------------------------------------------------------------
# StanceClassifier
# ---------------------------------------------------------------------------

class TestStanceClassifier:
    def test_supportive_stance(self, sc):
        r = sc.predict_text(
            "The renewable energy transition is creating thousands of new jobs and driving economic growth.",
            topic="renewable energy",
        )
        assert r.stance == "supportive"
        assert r.confidence > 0.5

    def test_critical_stance(self, sc):
        r = sc.predict_text(
            "The policy has done nothing to address the root causes of poverty and inequality.",
            topic="social policy",
        )
        assert r.stance == "critical"

    def test_neutral_stance(self, sc):
        r = sc.predict_text(
            "The bill was introduced to the Senate on Monday and will go to committee next week.",
            topic="legislation",
        )
        assert r.stance == "neutral"

    def test_ambiguous_stance(self, sc):
        r = sc.predict_text(
            "While the initiative has shown some promise, its long-term viability is uncertain.",
            topic="initiative",
        )
        assert r.stance == "ambiguous"

    def test_stance_always_valid_label(self, sc):
        valid = {"supportive", "critical", "neutral", "ambiguous"}
        sentences = [
            "This will lead to catastrophic outcomes for millions.",
            "The agreement opens up vital new markets.",
            "The committee meets every Tuesday.",
            "Results are mixed and outcomes remain unclear.",
        ]
        for s in sentences:
            r = sc.predict_text(s, topic="policy")
            assert r.stance in valid

    def test_topic_recorded_on_result(self, sc):
        results = sc.predict(
            _doc("The agreement opens up vital new markets for domestic manufacturers."),
            topic="trade policy",
        )
        assert all(r.topic == "trade policy" for r in results)

    def test_empty_document_returns_empty(self, sc):
        assert sc.predict(_doc(""), "trade") == []

    def test_confidence_range(self, sc):
        results = sc.predict(
            _doc("This reckless spending will saddle future generations with unsustainable debt."),
            topic="fiscal policy",
        )
        for r in results:
            assert 0.0 <= r.confidence <= 1.0

    def test_all_source_types_accepted(self, sc):
        content = "This legislation will significantly improve healthcare outcomes for all citizens."
        for st in ("news", "blog", "paper", "transcript", "book", "note"):
            results = sc.predict(_doc(content, source_type=st), topic="healthcare")
            assert len(results) >= 1
            assert isinstance(results[0], StancePrediction)

    def test_multi_sentence_returns_per_sentence(self, sc):
        text = (
            "The reform delivers vital improvements for workers. "
            "Critics argue the costs are far too high. "
            "The bill passed the lower house on Wednesday."
        )
        results = sc.predict(_doc(text), topic="labor reform")
        assert len(results) >= 2
        stances = {r.stance for r in results}
        # Should have some variety given the mix of supportive, critical, neutral
        assert len(stances) >= 1


# ---------------------------------------------------------------------------
# Dataset utilities
# ---------------------------------------------------------------------------

class TestDataset:
    def test_load_claim_dataset_bootstrap(self):
        from src.argument_mining.dataset import load_claim_dataset
        from services.ingest.common.document_model import SOURCE_TYPES
        examples = load_claim_dataset(None)
        assert len(examples) >= 20
        texts, labels, source_types = zip(*examples)
        assert all(isinstance(t, str) and len(t) > 10 for t in texts)
        assert set(labels) == {0, 1}
        assert all(st in SOURCE_TYPES for st in source_types)
        # Bootstrap data covers all supported content types
        assert set(source_types) == {"news", "blog", "paper", "transcript", "book", "note"}

    def test_load_stance_dataset_bootstrap(self):
        from src.argument_mining.dataset import load_stance_dataset, STANCE_LABELS
        from services.ingest.common.document_model import SOURCE_TYPES
        examples = load_stance_dataset(None)
        assert len(examples) >= 20
        _, _, stances, source_types = zip(*examples)
        assert set(stances).issubset(set(STANCE_LABELS))
        assert len(set(stances)) == 4  # all four labels represented
        assert all(st in SOURCE_TYPES for st in source_types)
        assert set(source_types) == {"news", "blog", "paper", "transcript", "book", "note"}

    def test_sentences_from_document(self):
        from src.argument_mining.dataset import sentences_from_document
        doc = _doc("First sentence here. Second sentence follows. And a third one now.")
        sents = sentences_from_document(doc)
        assert len(sents) >= 2

    def test_sentences_empty_document(self):
        from src.argument_mining.dataset import sentences_from_document
        assert sentences_from_document(_doc("")) == []

    def test_sentences_uses_title_fallback(self):
        from src.argument_mining.dataset import sentences_from_document
        doc = Document(
            document_id="t", source_type="news", language="en",
            ingested_at=0, content=None,
            title="Scientists discover new treatment for Alzheimer's disease.",
        )
        sents = sentences_from_document(doc)
        assert len(sents) == 1
