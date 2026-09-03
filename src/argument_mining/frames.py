"""
Narrative frame classifier for Noesis documents.

FrameClassifier — multi-label classifier returning a score (0–1) for each of
seven frames: economic / security / humanitarian / legal / political /
scientific / other.

Loads a fine-tuned distilbert-base-uncased checkpoint from
models/frame_classifier/ when available, otherwise the pinned pretrained NLI
checkpoint fetched by ``make models``.  It fails closed if neither is present.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from services.ingest.common.document_model import Document

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRAME_MODEL_DIR = _REPO_ROOT / "models" / "frame_classifier"

# Imported here so callers can do: from src.argument_mining.frames import FRAME_LABELS
from src.argument_mining.dataset import FRAME_LABELS  # noqa: E402
from src.argument_mining.models import _reject_removed_backend  # noqa: E402

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class FramePrediction:
    document_id: str
    source_type: str
    frames: Dict[str, float] = field(default_factory=dict)   # frame -> score 0–1
    dominant: str = "other"                                   # highest-scoring frame
    classified_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class FrameClassifier:
    """Multi-label narrative frame classifier.

    Returns a score per frame for the full document text.  A fine-tuned
    checkpoint takes precedence over the pinned pretrained NLI backend.
    """

    #: entailment hypothesis per frame for the zero-shot NLI backend (#955)
    NLI_TEMPLATES = {
        "economic": "This text frames the issue as an economic issue.",
        "security": "This text frames the issue as a security issue.",
        "humanitarian": "This text frames the issue as a humanitarian issue.",
        "legal": "This text frames the issue as a legal issue.",
        "political": "This text frames the issue as a political issue.",
        "scientific": "This text frames the issue as a scientific issue.",
        "other": "This text frames the issue outside economics, security, humanitarian, legal, political, or scientific terms.",
    }
    #: Per-label decision thresholds calibrated by the benchmark corpus.
    NLI_THRESHOLDS = {
        "political": 0.35,
        "humanitarian": 0.35,
    }
    NLI_DEFAULT_THRESHOLD = 0.45

    def __init__(self, model_dir: Optional[Path] = None, nli: Optional[Any] = None) -> None:
        _reject_removed_backend("NOESIS_FRAMES_BACKEND", "frame classification")
        self._model_dir = model_dir or _FRAME_MODEL_DIR
        self._pipeline = None
        self._nli = nli
        self._try_load()
        if self._pipeline is None and self._nli is None:
            self._try_load_nli()

    def _try_load_nli(self) -> None:
        """Use pinned zero-shot NLI by default when its weights are cached."""
        from src.kb.nli import TransformersNLI

        self._nli = TransformersNLI()
        logger.info("FrameClassifier: zero-shot NLI backend active (%s)",
                    self._nli.model_name)

    def _try_load(self) -> None:
        if not (self._model_dir / "config.json").exists():
            logger.info(
                "FrameClassifier: no fine-tuned model at %s; trying pinned NLI weights",
                self._model_dir,
            )
            return
        try:
            from transformers import pipeline as hf_pipeline
            self._pipeline = hf_pipeline(
                "text-classification",
                model=str(self._model_dir),
                tokenizer=str(self._model_dir),
                top_k=None,   # multi-label: return scores for all labels
                device=-1,
            )
            logger.info("FrameClassifier: loaded model from %s", self._model_dir)
        except Exception:
            logger.warning("FrameClassifier: fine-tuned model load failed", exc_info=True)

    @property
    def prediction_mode(self) -> str:
        """Return the active trained-model provenance."""
        if self._pipeline is not None:
            return f"model:{self._model_dir.name}"
        if self._nli is not None:
            return self._nli.prediction_mode
        raise RuntimeError("frame classifier has no active model backend")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def predict(self, document: Document) -> FramePrediction:
        """Return a FramePrediction for the full document."""
        text = _document_text(document)
        if not text.strip():
            return FramePrediction(
                document_id=document.document_id,
                source_type=document.source_type,
                frames={f: 0.0 for f in FRAME_LABELS},
                dominant="other",
            )
        if self._pipeline is not None:
            return self._predict_model(document, text)
        if self._nli is not None:
            return self._predict_nli(document, text)
        raise RuntimeError("frame classifier has no active model backend")

    def _predict_nli(self, document: Document, text: str) -> FramePrediction:
        """Multi-label frames via independent entailment per hypothesis.

        Each frame's score is its entailment confidence (0 when the model
        answers contradiction/neutral); a frame is *on* when its score
        clears its per-label threshold. Dominant = highest score, `other`
        when nothing clears.
        """
        snippet = text[:1500]
        scores: Dict[str, float] = {}
        labels = list(self.NLI_TEMPLATES)
        pairs = [(snippet, self.NLI_TEMPLATES[frame]) for frame in labels]
        batch_scores = (
            self._nli.entailment_scores(pairs)
            if hasattr(self._nli, "entailment_scores") else None
        )
        for index, frame in enumerate(labels):
            if batch_scores is not None:
                score = batch_scores[index]
            else:
                outcome = self._nli.classify(snippet, self.NLI_TEMPLATES[frame])
                score = outcome.confidence if outcome.label == "entailment" else 0.0
            threshold = self.NLI_THRESHOLDS.get(frame, self.NLI_DEFAULT_THRESHOLD)
            scores[frame] = round(score, 4) if score >= threshold else 0.0
        dominant = max(scores, key=scores.get)
        if scores[dominant] <= 0.0:
            dominant = "other"
        return FramePrediction(
            document_id=document.document_id,
            source_type=document.source_type,
            frames=scores,
            dominant=dominant,
        )

    def predict_text(self, text: str, source_type: str = "news") -> FramePrediction:
        """Convenience method for raw text."""
        import time
        doc = Document(
            document_id="__inline__",
            source_type=source_type,
            language="en",
            ingested_at=int(time.time() * 1000),
            content=text,
        )
        return self.predict(doc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _predict_model(self, document: Document, text: str) -> FramePrediction:
        raw = self._pipeline(text[:512], truncation=True)  # type: ignore[misc]
        # raw is a list of dicts with "label" and "score"
        frames: Dict[str, float] = {}
        for item in raw:
            label = item["label"].lower().replace("label_", "")
            # Map numeric label ids to frame names if needed
            try:
                idx = int(label)
                label = FRAME_LABELS[idx]
            except ValueError:
                pass  # label is already a name
            frames[label] = float(item["score"])
        # Ensure all frames present
        for f in FRAME_LABELS:
            frames.setdefault(f, 0.0)
        dominant = max(frames, key=frames.__getitem__)
        return FramePrediction(
            document_id=document.document_id,
            source_type=document.source_type,
            frames=frames,
            dominant=dominant,
        )


def _document_text(doc: Document) -> str:
    """Concatenate title + content for frame scoring."""
    parts = []
    if doc.title:
        parts.append(doc.title)
    if doc.content:
        parts.append(doc.content)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Enrichment helpers (post-ingestion stage)
# ---------------------------------------------------------------------------

def store_document_frames(prediction: FramePrediction, conn) -> None:
    """Write FramePrediction scores to the document_frames DuckDB table."""
    ts = prediction.classified_at.isoformat()
    for frame, score in prediction.frames.items():
        conn.execute(
            """
            INSERT OR REPLACE INTO document_frames
                (document_id, source_type, frame, score, classified_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [prediction.document_id, prediction.source_type, frame, score, ts],
        )


def classify_and_store(document: Document, conn) -> FramePrediction:
    """Classify a document's frames and persist the result."""
    prediction = get_frame_classifier().predict(document)
    store_document_frames(prediction, conn)
    return prediction


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_frame_classifier: Optional[FrameClassifier] = None


def get_frame_classifier() -> FrameClassifier:
    global _frame_classifier
    if _frame_classifier is None:
        _frame_classifier = FrameClassifier()
    return _frame_classifier


def predict_frames(document: Document) -> FramePrediction:
    """Module-level convenience: predict frames for any Document."""
    return get_frame_classifier().predict(document)
