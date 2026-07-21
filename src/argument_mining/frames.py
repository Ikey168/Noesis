"""
Narrative frame classifier for Noesis documents.

FrameClassifier — multi-label classifier returning a score (0–1) for each of
seven frames: economic / security / humanitarian / legal / political /
scientific / other.

Loads a fine-tuned distilbert-base-uncased checkpoint from
models/frame_classifier/ when available; falls back to a keyword-density
heuristic otherwise so the rest of the pipeline works without a training step.
"""
from __future__ import annotations

import logging
import re
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
# Keyword sets for the heuristic fallback
# ---------------------------------------------------------------------------

_FRAME_KEYWORDS: Dict[str, frozenset] = {
    "economic": frozenset({
        "market", "markets", "revenue", "profit", "gdp", "trade", "budget", "tax", "tariff",
        "investment", "inflation", "unemployment", "debt", "fiscal", "monetary",
        "economic", "financial", "economy", "bank", "banking", "export", "import",
        "spending", "recession", "growth", "price", "prices", "wage", "wages",
        "cost", "costs", "stock", "bond", "treasury", "currency", "rate", "rates",
        "subsidy", "earnings", "surplus", "deficit",
    }),
    "security": frozenset({
        "military", "weapon", "army", "navy", "attack", "threat", "war", "soldier",
        "combat", "defence", "defense", "intelligence", "terrorism", "nuclear",
        "missile", "border", "crime", "violence", "troops", "force", "armed",
        "police", "surveillance", "cybersecurity", "breach", "raid", "hostage",
        "extremism", "espionage", "deterrence", "arsenal", "battalion",
    }),
    "humanitarian": frozenset({
        "refugee", "poverty", "hunger", "humanitarian", "aid", "rights",
        "displacement", "victim", "civilian", "relief", "shelter", "suffering",
        "vulnerable", "child", "food", "water", "emergency", "evacuation",
        "displaced", "dignity", "trauma", "famine", "charity", "orphan",
        "sanitation", "malnutrition", "asylum", "stateless", "persecution",
    }),
    "legal": frozenset({
        "court", "lawsuit", "law", "regulation", "legislation", "ruling", "judge",
        "attorney", "compliance", "statute", "contract", "liability", "enforcement",
        "prosecution", "verdict", "constitutional", "jurisdiction", "treaty",
        "amendment", "plaintiff", "defendant", "penalty", "sentence", "litigation",
        "appeal", "legal", "legislation", "injunction", "subpoena", "indictment",
    }),
    "political": frozenset({
        "election", "government", "parliament", "senate", "party", "vote",
        "president", "minister", "diplomacy", "coalition", "opposition",
        "administration", "democracy", "congress", "governor", "cabinet",
        "political", "campaign", "ballot", "reform", "diplomat", "ambassador",
        "referendum", "sanctions", "geopolitical", "partisan", "constituency",
    }),
    "scientific": frozenset({
        "research", "study", "data", "experiment", "findings", "analysis",
        "evidence", "hypothesis", "methodology", "trial", "laboratory",
        "publication", "statistics", "model", "theory", "discovery", "innovation",
        "algorithm", "simulation", "sample", "cohort", "clinical", "measurement",
        "peer", "journal", "dataset", "scientific", "correlation", "regression",
        "genome", "protein", "neural", "quantum",
    }),
    "other": frozenset({
        "community", "culture", "art", "music", "sport", "entertainment",
        "religion", "education", "travel", "family", "lifestyle", "sports",
        "festival", "celebrity", "fashion", "tourism", "recreational", "personal",
    }),
}

FRAME_THRESHOLD = 0.40   # score above which a frame is considered active


def _frame_heuristic(text: str, doc_id: str, source_type: str) -> FramePrediction:
    """Keyword-density frame scorer used when no model is trained."""
    t = text.lower()
    words = set(re.findall(r"\b\w+\b", t))

    scores: Dict[str, float] = {}
    for frame, keywords in _FRAME_KEYWORDS.items():
        if frame == "other":
            continue
        matches = len(words & keywords)
        # Map match counts to 0–0.90 using a rough step function
        scores[frame] = min(0.90, 0.15 + matches * 0.20)

    # "other" scores high only when no specific frame exceeds the threshold
    top_specific = max(scores.values()) if scores else 0.0
    scores["other"] = 0.70 if top_specific < 0.25 else 0.12

    dominant = max(scores, key=scores.__getitem__)
    return FramePrediction(
        document_id=doc_id,
        source_type=source_type,
        frames=scores,
        dominant=dominant,
    )


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class FrameClassifier:
    """Multi-label narrative frame classifier.

    Returns a score per frame for the full document text.  When a fine-tuned
    checkpoint exists in models/frame_classifier/ it is used; otherwise the
    keyword heuristic runs.
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
    #: per-label decision thresholds — deliberately permissive for the frames
    #: the heuristic starved of recall (political 0.16, humanitarian 0.25)
    NLI_THRESHOLDS = {
        "political": 0.35,
        "humanitarian": 0.35,
    }
    NLI_DEFAULT_THRESHOLD = 0.45

    def __init__(self, model_dir: Optional[Path] = None, nli: Optional[Any] = None) -> None:
        self._model_dir = model_dir or _FRAME_MODEL_DIR
        self._pipeline = None
        self._nli = nli
        self._try_load()
        if self._pipeline is None and self._nli is None:
            self._try_load_nli()

    def _try_load_nli(self) -> None:
        """Zero-shot NLI backend, opt-in via NOESIS_FRAMES_BACKEND=nli.

        Opt-in because loading the pinned cross-encoder can trigger a model
        download; the fetch flow (#959) prepares the cache first.
        """
        import os

        if os.environ.get("NOESIS_FRAMES_BACKEND", "").lower() != "nli":
            return
        try:
            from src.kb.nli import TransformersNLI

            self._nli = TransformersNLI()
            logger.info("FrameClassifier: zero-shot NLI backend active (%s)",
                        self._nli.model_name)
        except Exception:
            logger.warning(
                "FrameClassifier: NLI backend unavailable — using heuristic fallback",
                exc_info=True,
            )

    def _try_load(self) -> None:
        if not (self._model_dir / "config.json").exists():
            logger.info(
                "FrameClassifier: no trained model at %s — using heuristic fallback",
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
            logger.warning("FrameClassifier: load failed — using heuristic fallback", exc_info=True)

    @property
    def prediction_mode(self) -> str:
        """`model:<dir>` | `zero-shot:<model>` | `heuristic` (#958, #955)."""
        if self._pipeline is not None:
            return f"model:{self._model_dir.name}"
        if self._nli is not None:
            return self._nli.prediction_mode
        return "heuristic"

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
        return _frame_heuristic(text, document.document_id, document.source_type)

    def _predict_nli(self, document: Document, text: str) -> FramePrediction:
        """Multi-label frames via independent entailment per hypothesis.

        Each frame's score is its entailment confidence (0 when the model
        answers contradiction/neutral); a frame is *on* when its score
        clears its per-label threshold. Dominant = highest score, `other`
        when nothing clears — the same shape the heuristic produces, so
        downstream consumers are agnostic.
        """
        snippet = text[:1500]
        scores: Dict[str, float] = {}
        for frame, hypothesis in self.NLI_TEMPLATES.items():
            outcome = self._nli.classify(snippet, hypothesis)
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
