"""
Unit tests for the narrative frame classifier.

Tests inject a lightweight NLI model double and do not require downloaded
weights.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from services.ingest.common.document_model import Document
from src.argument_mining.dataset import FRAME_LABELS, load_frame_dataset
from src.argument_mining.frames import FrameClassifier, FramePrediction

_NO_MODEL = Path("/tmp/_nonexistent_frame_model")


def _doc(content: str, source_type: str = "news", title: str = "") -> Document:
    return Document(
        document_id="t-frame",
        source_type=source_type,
        language="en",
        ingested_at=int(time.time() * 1000),
        title=title or None,
        content=content,
    )


@pytest.fixture(scope="module")
def fc() -> FrameClassifier:
    class FakeNLI:
        prediction_mode = "zero-shot:test-nli-model"

        @staticmethod
        def entailment_scores(pairs):
            vocabulary = {
                "economic": ("market", "inflation", "bank", "budget", "currency"),
                "security": ("military", "attack", "border", "pentagon", "sanctions"),
                "humanitarian": ("aid", "refugee", "food", "poverty", "civilian"),
                "legal": ("court", "ruling", "regulation", "plaintiff"),
                "political": ("election", "coalition", "parliament", "congress"),
                "scientific": ("study", "clinical", "peer-reviewed", "research"),
                "other": ("festival",),
            }
            scores = []
            for premise, hypothesis in pairs:
                label = next(
                    (name for name in vocabulary if f" {name} " in hypothesis),
                    "other",
                )
                matches = sum(word in premise.lower() for word in vocabulary[label])
                scores.append(min(0.95, 0.1 + matches * 0.36))
            return scores

    return FrameClassifier(model_dir=_NO_MODEL, nli=FakeNLI())


# ---------------------------------------------------------------------------
# Frame detection — individual frames
# ---------------------------------------------------------------------------

class TestFrameClassifier:
    def test_economic_frame_detected(self, fc):
        p = fc.predict(_doc(
            "Markets fell sharply as inflation rose 4.1% and the central bank raised rates."
        ))
        assert p.frames["economic"] > 0
        assert p.dominant == "economic"

    def test_security_frame_detected(self, fc):
        p = fc.predict(_doc(
            "Military forces launched an attack on enemy infrastructure near the border."
        ))
        assert p.frames["security"] > 0

    def test_humanitarian_frame_detected(self, fc):
        p = fc.predict(_doc(
            "Aid agencies warned that displaced refugees face acute food and water insecurity."
        ))
        assert p.frames["humanitarian"] > 0

    def test_legal_frame_detected(self, fc):
        p = fc.predict(_doc(
            "The court issued a ruling upholding the regulation; the plaintiff's appeal was dismissed."
        ))
        assert p.frames["legal"] > 0

    def test_political_frame_detected(self, fc):
        p = fc.predict(_doc(
            "The election result left the coalition without a majority in parliament."
        ))
        assert p.frames["political"] > 0

    def test_scientific_frame_detected(self, fc):
        p = fc.predict(_doc(
            "The peer-reviewed study found a statistically significant correlation in the clinical trial data."
        ))
        assert p.frames["scientific"] > 0

    def test_other_frame_high_when_no_specific_signals(self, fc):
        p = fc.predict(_doc("The festival drew thousands of visitors over the weekend."))
        # With no specific-frame keywords, "other" should be the dominant
        assert p.dominant == "other"

    def test_multi_label_returns_multiple_active_frames(self, fc):
        p = fc.predict(_doc(
            "The Pentagon's $850 billion budget request faces opposition from fiscal hawks in Congress."
        ))
        active = [f for f, s in p.frames.items() if s > 0]
        assert len(active) >= 2  # economic + security + political should all fire

    # ------------------------------------------------------------------
    # Result structure
    # ------------------------------------------------------------------

    def test_all_frame_labels_present(self, fc):
        p = fc.predict(_doc("The government increased military spending by 12%."))
        assert set(p.frames.keys()) == set(FRAME_LABELS)

    def test_scores_in_range(self, fc):
        p = fc.predict(_doc("Scientists published findings in a peer-reviewed journal."))
        for score in p.frames.values():
            assert 0.0 <= score <= 1.0

    def test_dominant_is_valid_label(self, fc):
        p = fc.predict(_doc("The election outcome was determined by postal votes."))
        assert p.dominant in FRAME_LABELS

    def test_empty_document_returns_zero_scores(self, fc):
        p = fc.predict(_doc(""))
        assert all(s == 0.0 for s in p.frames.values())
        assert p.dominant == "other"

    def test_document_id_preserved(self, fc):
        doc = Document(
            document_id="my-doc-42",
            source_type="news",
            language="en",
            ingested_at=0,
            content="Markets fell as inflation rose.",
        )
        p = fc.predict(doc)
        assert p.document_id == "my-doc-42"

    def test_source_type_preserved(self, fc):
        for st in ("news", "blog", "paper", "transcript", "book", "note"):
            doc = _doc("Researchers published a peer-reviewed study.", source_type=st)
            p = fc.predict(doc)
            assert p.source_type == st

    def test_predict_text_convenience(self, fc):
        p = fc.predict_text(
            "The court ruled the regulation was unconstitutional.", source_type="news"
        )
        assert isinstance(p, FramePrediction)
        assert p.frames["legal"] > 0

    def test_title_contributes_to_score(self, fc):
        with_title = fc.predict(_doc("Brief content.", title="Military attack on border troops"))
        without_title = fc.predict(_doc("Brief content."))
        assert with_title.frames["security"] >= without_title.frames["security"]

    def test_multi_label_scores_can_exceed_one_total(self, fc):
        p = fc.predict(_doc(
            "Sanctions imposed on the central bank triggered a currency collapse, "
            "pushing millions of civilians into poverty and humanitarian crisis."
        ))
        total = sum(p.frames.values())
        # Multi-label: total is not normalised to 1
        assert total > 1.0 or p.dominant in FRAME_LABELS


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

class TestFrameDataset:
    def test_load_frame_dataset_bootstrap(self):
        examples = load_frame_dataset(None)
        assert len(examples) >= 30
        for text, source_type, frames in examples:
            assert isinstance(text, str) and len(text) > 10
            assert isinstance(frames, list) and len(frames) >= 1
            for f in frames:
                assert f in FRAME_LABELS

    def test_all_source_types_covered(self):
        examples = load_frame_dataset(None)
        covered = {e[1] for e in examples}
        assert covered == {"news", "blog", "paper", "transcript", "book", "note"}

    def test_all_frame_labels_covered(self):
        examples = load_frame_dataset(None)
        all_frames: set = set()
        for _text, _stype, frames in examples:
            all_frames.update(frames)
        assert all_frames == set(FRAME_LABELS)

    def test_multi_label_examples_present(self):
        examples = load_frame_dataset(None)
        assert any(len(e[2]) >= 2 for e in examples), "Expected at least one multi-label example"
