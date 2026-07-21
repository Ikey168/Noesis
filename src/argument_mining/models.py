"""
Inference wrappers for argument mining models.

ClaimDetector      — binary sentence-level claim detection
StanceClassifier   — 4-class stance (supportive / critical / neutral / ambiguous)

Both classes attempt to load fine-tuned weights from models/ on first use.
When no trained model is present they fall back to a rule-based heuristic so
the rest of the pipeline can run during development without a training step.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from services.ingest.common.document_model import Document
from src.argument_mining.dataset import sentences_from_document, ID2STANCE

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLAIM_MODEL_DIR = _REPO_ROOT / "models" / "claim_detector"
_STANCE_MODEL_DIR = _REPO_ROOT / "models" / "stance_classifier"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ClaimPrediction:
    text: str
    sentence_idx: int
    is_claim: bool
    confidence: float   # probability of the predicted class, 0.0–1.0


@dataclass
class StancePrediction:
    text: str
    sentence_idx: int
    topic: str
    stance: str         # supportive | critical | neutral | ambiguous
    confidence: float


# ---------------------------------------------------------------------------
# Claim Detector
# ---------------------------------------------------------------------------

class ClaimDetector:
    """Sentence-level binary classifier: factual claim vs. opinion/background.

    Loads a fine-tuned ``distilbert-base-uncased`` checkpoint from
    ``models/claim_detector/`` when available.  Falls back to a lightweight
    heuristic otherwise.
    """

    def __init__(self, model_dir: Optional[Path] = None) -> None:
        self._model_dir = model_dir or _CLAIM_MODEL_DIR
        self._pipeline = None
        self._try_load()

    def _try_load(self) -> None:
        if not (self._model_dir / "config.json").exists():
            logger.info(
                "ClaimDetector: no trained model at %s — using heuristic fallback",
                self._model_dir,
            )
            return
        try:
            from transformers import pipeline as hf_pipeline
            self._pipeline = hf_pipeline(
                "text-classification",
                model=str(self._model_dir),
                tokenizer=str(self._model_dir),
                device=-1,
            )
            logger.info("ClaimDetector: loaded model from %s", self._model_dir)
        except Exception:
            logger.warning("ClaimDetector: load failed — using heuristic fallback", exc_info=True)


    @property
    def prediction_mode(self) -> str:
        """`model:<dir>` when a checkpoint is active, else `heuristic` (#958)."""
        if self._pipeline is not None:
            return f"model:{self._model_dir.name}"
        return "heuristic"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def predict(self, document: Document) -> List[ClaimPrediction]:
        """Return one ClaimPrediction per sentence in the document."""
        sentences = sentences_from_document(document)
        if not sentences:
            return []
        if self._pipeline is not None:
            return self._predict_model(sentences)
        return [_claim_heuristic(s, i) for i, s in enumerate(sentences)]

    def predict_text(self, text: str) -> ClaimPrediction:
        """Convenience method for a single sentence (useful for tests and API)."""
        doc = Document(
            document_id="__inline__",
            source_type="news",
            language="en",
            ingested_at=0,
            content=text,
        )
        results = self.predict(doc)
        return results[0] if results else ClaimPrediction(
            text=text, sentence_idx=0, is_claim=False, confidence=0.5
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _predict_model(self, sentences: List[str]) -> List[ClaimPrediction]:
        batch = self._pipeline(
            sentences, truncation=True, max_length=128, batch_size=16
        )
        results = []
        for i, (sent, pred) in enumerate(zip(sentences, batch)):
            is_claim = pred["label"] == "LABEL_1"
            raw_score = pred["score"]
            results.append(ClaimPrediction(
                text=sent,
                sentence_idx=i,
                is_claim=is_claim,
                confidence=raw_score if is_claim else 1.0 - raw_score,
            ))
        return results


def _claim_heuristic(text: str, idx: int) -> ClaimPrediction:
    """Lightweight rule-based claim detector used when no model is trained."""
    t = text.lower()

    score = 0.5

    # Positive signals
    if re.search(r"\b\d+(\.\d+)?\s*(%|bn|million|thousand|°c|km|mg|hz)\b", t):
        score += 0.20  # specific measurement
    if re.search(r"\b\d{4}\b", t) and re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|december|monday|tuesday|wednesday|thursday|friday)\b", t):
        score += 0.10  # dated event
    if re.search(r"\b(was|were|had|said|reported|found|showed|rose|fell|signed|passed|ruled|confirmed|announced|published|identified|collapsed|resigned|died|won)\b", t):
        score += 0.15  # past-tense active verb
    if re.search(r"\b(the (government|court|company|bank|university|study|report|institute|agency|committee))\b", t):
        score += 0.10  # institutional subject

    # Negative signals
    if re.search(r"\b(may|might|could|would|perhaps|possibly|seem|appear|believe|think|feel|argue|suggest|worry|hope|fear|expect)\b", t):
        score -= 0.20  # epistemic hedging
    if text.strip().endswith("?"):
        score -= 0.30  # question
    if re.search(r"\b(i|we|our|my)\b", t):
        score -= 0.15  # first person
    if re.search(r"^(in my|in our|many (people|observers|analysts|experts) (believe|think|say|argue)|it remains|the question|critics|supporters)", t):
        score -= 0.20  # opinion opener

    score = max(0.05, min(0.95, score))
    is_claim = score >= 0.5
    return ClaimPrediction(
        text=text,
        sentence_idx=idx,
        is_claim=is_claim,
        confidence=score if is_claim else 1.0 - score,
    )


# ---------------------------------------------------------------------------
# Stance Classifier
# ---------------------------------------------------------------------------

class StanceClassifier:
    """4-class stance classifier: supportive / critical / neutral / ambiguous.

    Loads a fine-tuned ``distilbert-base-uncased`` checkpoint from
    ``models/stance_classifier/`` when available.  Falls back to a
    keyword-based heuristic otherwise.
    """

    def __init__(self, model_dir: Optional[Path] = None) -> None:
        self._model_dir = model_dir or _STANCE_MODEL_DIR
        self._pipeline = None
        self._try_load()

    def _try_load(self) -> None:
        if not (self._model_dir / "config.json").exists():
            logger.info(
                "StanceClassifier: no trained model at %s — using heuristic fallback",
                self._model_dir,
            )
            return
        try:
            from transformers import pipeline as hf_pipeline
            self._pipeline = hf_pipeline(
                "text-classification",
                model=str(self._model_dir),
                tokenizer=str(self._model_dir),
                device=-1,
            )
            logger.info("StanceClassifier: loaded model from %s", self._model_dir)
        except Exception:
            logger.warning("StanceClassifier: load failed — using heuristic fallback", exc_info=True)


    @property
    def prediction_mode(self) -> str:
        """`model:<dir>` when a checkpoint is active, else `heuristic` (#958)."""
        if self._pipeline is not None:
            return f"model:{self._model_dir.name}"
        return "heuristic"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def predict(self, document: Document, topic: str) -> List[StancePrediction]:
        """Return one StancePrediction per sentence in the document."""
        sentences = sentences_from_document(document)
        if not sentences:
            return []
        if self._pipeline is not None:
            return self._predict_model(sentences, topic)
        return [_stance_heuristic(s, i, topic) for i, s in enumerate(sentences)]

    def predict_text(self, text: str, topic: str) -> StancePrediction:
        """Convenience method for a single sentence."""
        doc = Document(
            document_id="__inline__",
            source_type="news",
            language="en",
            ingested_at=0,
            content=text,
        )
        results = self.predict(doc, topic)
        return results[0] if results else StancePrediction(
            text=text, sentence_idx=0, topic=topic, stance="neutral", confidence=0.5
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _predict_model(self, sentences: List[str], topic: str) -> List[StancePrediction]:
        # Prepend topic so the model sees topical context
        inputs = [f"{topic} [SEP] {s}" for s in sentences]
        batch = self._pipeline(inputs, truncation=True, max_length=128, batch_size=16)
        results = []
        for i, (sent, pred) in enumerate(zip(sentences, batch)):
            label_idx = int(pred["label"].split("_")[1])
            stance = ID2STANCE.get(label_idx, "neutral")
            results.append(StancePrediction(
                text=sent,
                sentence_idx=i,
                topic=topic,
                stance=stance,
                confidence=pred["score"],
            ))
        return results


_POSITIVE_WORDS = {
    "benefit", "improve", "growth", "success", "effective", "landmark",
    "significant", "remarkable", "protect", "advance", "support", "welcome",
    "achievement", "opportunity", "innovative", "strengthen", "deliver",
    "transform", "vital", "essential", "empower", "robust", "thriving",
}
_NEGATIVE_WORDS = {
    "fail", "failed", "failure", "inadequate", "reckless", "devastating", "crisis",
    "burden", "sacrifice", "unsustainable", "worsen", "damage", "harm", "threaten",
    "corrupt", "waste", "ineffective", "undermine", "collapse", "neglect", "betrayal",
    "disastrous", "catastrophic", "unacceptable", "shameful", "poverty", "inequality",
    "nothing", "lacking", "absent", "refused", "neglected", "exploit", "oppress",
    "mismanage", "broken", "flawed", "disregard", "ignore",
}
_NEUTRAL_WORDS = {
    "reported", "announced", "said", "according", "data", "figure",
    "scheduled", "voted", "signed", "published", "released", "stated",
    "confirmed", "outlined", "presented", "introduced", "noted",
}
_HEDGE_PHRASES = [
    "while", "although", "however", "mixed", "some areas", "uncertain",
    "remains to be", "difficult to", "on the other hand", "both",
    "some promise", "long-term viability", "but critics", "supporters point",
    "it is unclear", "remains unclear",
]


def _stance_heuristic(text: str, idx: int, topic: str) -> StancePrediction:
    t = text.lower()
    words = set(re.findall(r"\b\w+\b", t))

    pos = len(words & _POSITIVE_WORDS)
    neg = len(words & _NEGATIVE_WORDS)
    neu = len(words & _NEUTRAL_WORDS)
    hedge = sum(1 for ph in _HEDGE_PHRASES if ph in t)

    # Ambiguous: explicit hedging, or balanced pos/neg signals
    if hedge >= 1 and pos == 0 and neg == 0:
        return StancePrediction(text=text, sentence_idx=idx, topic=topic,
                                stance="ambiguous", confidence=0.55)
    if (pos > 0 and neg > 0) or hedge >= 2:
        return StancePrediction(text=text, sentence_idx=idx, topic=topic,
                                stance="ambiguous", confidence=0.55)
    if neu >= 2 and pos == 0 and neg == 0:
        return StancePrediction(text=text, sentence_idx=idx, topic=topic,
                                stance="neutral", confidence=0.65)
    if pos > neg:
        return StancePrediction(text=text, sentence_idx=idx, topic=topic,
                                stance="supportive",
                                confidence=min(0.90, 0.55 + pos * 0.10))
    if neg > pos:
        return StancePrediction(text=text, sentence_idx=idx, topic=topic,
                                stance="critical",
                                confidence=min(0.90, 0.55 + neg * 0.10))
    return StancePrediction(text=text, sentence_idx=idx, topic=topic,
                            stance="neutral", confidence=0.50)


# ---------------------------------------------------------------------------
# Module-level singletons (lazy-initialised)
# ---------------------------------------------------------------------------

_claim_detector: Optional[ClaimDetector] = None
_stance_classifier: Optional[StanceClassifier] = None


def get_claim_detector() -> ClaimDetector:
    global _claim_detector
    if _claim_detector is None:
        _claim_detector = ClaimDetector()
    return _claim_detector


def get_stance_classifier() -> StanceClassifier:
    global _stance_classifier
    if _stance_classifier is None:
        _stance_classifier = StanceClassifier()
    return _stance_classifier


def predict_claims(document: Document) -> List[ClaimPrediction]:
    """Module-level convenience: predict claims for any Document."""
    return get_claim_detector().predict(document)


def predict_stance(document: Document, topic: str) -> List[StancePrediction]:
    """Module-level convenience: predict stance for any Document + topic."""
    return get_stance_classifier().predict(document, topic)
