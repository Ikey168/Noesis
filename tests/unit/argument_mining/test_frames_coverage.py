"""
Coverage-focused unit tests for src/argument_mining/frames.py.

Targets the model-backed prediction path (_try_load + _predict_model),
fail-closed loading, DuckDB persistence helpers, and module-level helpers.

The transformers pipeline is a heavy dependency; the module resolves it via
``from transformers import pipeline`` inside _try_load, so we inject a stub
``transformers`` module into sys.modules (transformers' lazy loader ignores
attribute patching).  A valid config.json in a temp dir makes _try_load take
the model branch.
"""
from __future__ import annotations

import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

import src.argument_mining.frames as frames_mod
from src.argument_mining.frames import (
    FrameClassifier,
    FramePrediction,
    classify_and_store,
    get_frame_classifier,
    predict_frames,
    store_document_frames,
)
from src.argument_mining.dataset import FRAME_LABELS
from services.ingest.common.document_model import Document

duckdb = pytest.importorskip("duckdb")


def _doc(content: str, source_type: str = "news", title: str | None = None) -> Document:
    return Document(
        document_id="doc-frame",
        source_type=source_type,
        language="en",
        ingested_at=0,
        title=title,
        content=content,
    )


def _stub_transformers(pipe_callable):
    """Return a fake ``transformers`` module whose ``pipeline`` returns pipe_callable."""
    mod = types.ModuleType("transformers")
    mod.pipeline = lambda *args, **kwargs: pipe_callable
    return {"transformers": mod}


def _model_dir_with_config() -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "config.json").write_text("{}")
    return d


def _frame_double() -> FrameClassifier:
    class NLI:
        prediction_mode = "zero-shot:test-nli-model"

        @staticmethod
        def entailment_scores(pairs):
            return [0.9 if "economic" in hypothesis else 0.1
                    for _premise, hypothesis in pairs]

    return FrameClassifier(nli=NLI())


# ---------------------------------------------------------------------------
# Model-backed prediction path
# ---------------------------------------------------------------------------

class TestModelPredictionPath:
    def test_named_labels_mapped_to_frames(self):
        model_dir = _model_dir_with_config()

        def fake_pipe(text, **kwargs):
            return [
                {"label": "economic", "score": 0.81},
                {"label": "legal", "score": 0.42},
            ]

        with mock.patch.dict(sys.modules, _stub_transformers(fake_pipe)):
            fc = FrameClassifier(model_dir=model_dir)
            assert fc._pipeline is not None
            pred = fc.predict(_doc("Markets and courts collided over the merger deal today."))

        assert pred.frames["economic"] == pytest.approx(0.81)
        assert pred.frames["legal"] == pytest.approx(0.42)
        # unlisted frames filled with 0.0
        assert pred.frames["security"] == 0.0
        assert set(pred.frames.keys()) == set(FRAME_LABELS)
        assert pred.dominant == "economic"

    def test_numeric_label_ids_mapped_via_frame_labels(self):
        model_dir = _model_dir_with_config()

        # LABEL_1 -> "label_1" -> stripped "1" -> FRAME_LABELS[1] == "security"
        def fake_pipe(text, **kwargs):
            return [
                {"label": "LABEL_1", "score": 0.9},
                {"label": "LABEL_0", "score": 0.3},
            ]

        with mock.patch.dict(sys.modules, _stub_transformers(fake_pipe)):
            fc = FrameClassifier(model_dir=model_dir)
            pred = fc.predict(_doc("An article with strong signals to classify here."))

        assert pred.frames["security"] == pytest.approx(0.9)   # index 1
        assert pred.frames["economic"] == pytest.approx(0.3)   # index 0
        assert pred.dominant == "security"

    def test_predict_text_uses_model_path(self):
        model_dir = _model_dir_with_config()

        def fake_pipe(text, **kwargs):
            return [{"label": "scientific", "score": 0.77}]

        with mock.patch.dict(sys.modules, _stub_transformers(fake_pipe)):
            fc = FrameClassifier(model_dir=model_dir)
            pred = fc.predict_text("A peer-reviewed study of clinical trial data.", source_type="paper")

        assert isinstance(pred, FramePrediction)
        assert pred.source_type == "paper"
        assert pred.frames["scientific"] == pytest.approx(0.77)

    def test_empty_document_short_circuits_before_model(self):
        model_dir = _model_dir_with_config()

        def fake_pipe(text, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("model should not run on empty text")

        with mock.patch.dict(sys.modules, _stub_transformers(fake_pipe)):
            fc = FrameClassifier(model_dir=model_dir)
            pred = fc.predict(_doc(""))

        assert all(score == 0.0 for score in pred.frames.values())
        assert pred.dominant == "other"


# ---------------------------------------------------------------------------
# _try_load failure handling
# ---------------------------------------------------------------------------

class TestTryLoadFailure:
    def test_pipeline_construction_error_fails_closed(self):
        model_dir = _model_dir_with_config()

        def boom_pipeline(*args, **kwargs):
            raise RuntimeError("cannot load weights")

        mod = types.ModuleType("transformers")
        mod.pipeline = boom_pipeline
        with mock.patch.dict(sys.modules, {"transformers": mod}), pytest.raises(RuntimeError):
            FrameClassifier(model_dir=model_dir)

    def test_removed_backend_setting_is_rejected(self, monkeypatch):
        monkeypatch.setenv("NOESIS_FRAMES_BACKEND", "heuristic")
        with pytest.raises(ValueError, match="has been removed"):
            FrameClassifier(model_dir=Path("/tmp/_nonexistent_frame_model_dir"))


# ---------------------------------------------------------------------------
# DuckDB persistence
# ---------------------------------------------------------------------------

def _frames_conn():
    conn = duckdb.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE document_frames (
            document_id VARCHAR,
            source_type VARCHAR,
            frame VARCHAR,
            score DOUBLE,
            classified_at VARCHAR,
            PRIMARY KEY (document_id, frame)
        )
        """
    )
    return conn


class TestPersistence:
    def test_store_document_frames_writes_each_frame(self):
        conn = _frames_conn()
        pred = FramePrediction(
            document_id="d1",
            source_type="news",
            frames={"economic": 0.8, "other": 0.1},
            dominant="economic",
            classified_at=datetime(2024, 1, 1, 12, 0, 0),
        )
        store_document_frames(pred, conn)
        rows = conn.execute(
            "SELECT frame, score, classified_at FROM document_frames "
            "WHERE document_id = 'd1' ORDER BY frame"
        ).fetchall()
        assert rows[0][0] == "economic"
        assert rows[0][1] == pytest.approx(0.8)
        assert rows[0][2] == "2024-01-01T12:00:00"
        assert rows[1][0] == "other"

    def test_store_is_idempotent(self):
        conn = _frames_conn()
        pred = FramePrediction(
            document_id="d1",
            source_type="news",
            frames={"economic": 0.8, "other": 0.1},
            dominant="economic",
            classified_at=datetime(2024, 1, 1),
        )
        store_document_frames(pred, conn)
        store_document_frames(pred, conn)  # INSERT OR REPLACE
        count = conn.execute(
            "SELECT COUNT(*) FROM document_frames WHERE document_id = 'd1'"
        ).fetchone()[0]
        assert count == 2  # two frames, not four

    def test_classify_and_store_persists_all_frame_labels(self):
        conn = _frames_conn()
        frames_mod._frame_classifier = _frame_double()
        try:
            doc = _doc(
                "Markets fell as inflation rose and the central bank raised rates sharply."
            )
            pred = classify_and_store(doc, conn)
            assert pred.dominant == "economic"
            count = conn.execute(
                "SELECT COUNT(*) FROM document_frames WHERE document_id = ?",
                [doc.document_id],
            ).fetchone()[0]
            assert count == len(FRAME_LABELS)
        finally:
            frames_mod._frame_classifier = None


# ---------------------------------------------------------------------------
# Module-level singleton / convenience
# ---------------------------------------------------------------------------

class TestModuleHelpers:
    def test_get_frame_classifier_is_singleton(self):
        frames_mod._frame_classifier = _frame_double()
        try:
            first = get_frame_classifier()
            second = get_frame_classifier()
            assert first is second
            assert isinstance(first, FrameClassifier)
        finally:
            frames_mod._frame_classifier = None

    def test_predict_frames_convenience(self):
        frames_mod._frame_classifier = _frame_double()
        try:
            pred = predict_frames(
                _doc("Researchers published peer-reviewed findings from a clinical study.")
            )
            assert isinstance(pred, FramePrediction)
            assert pred.frames["economic"] > 0.0
        finally:
            frames_mod._frame_classifier = None
