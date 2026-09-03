"""
Inference wrappers for argument mining models.

ClaimDetector      — binary sentence-level claim detection
StanceClassifier   — 4-class stance (supportive / critical / neutral / ambiguous)

Both classes attempt to load fine-tuned weights from models/ on first use,
then use the pinned pretrained backend fetched by ``make models``.  They fail
closed when neither model backend is available.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from services.ingest.common.document_model import Document
from src.argument_mining.dataset import sentences_from_document, ID2STANCE

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLAIM_MODEL_DIR = _REPO_ROOT / "models" / "claim_detector"
_STANCE_MODEL_DIR = _REPO_ROOT / "models" / "stance_classifier"


def _reject_removed_backend(env_name: str, surface: str) -> None:
    if os.environ.get(env_name, "auto").lower() in {"heuristic", "off", "disabled"}:
        raise ValueError(
            f"heuristic {surface} has been removed; use the pinned pretrained backend"
        )


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
    ``models/claim_detector/`` when available, otherwise uses the pinned
    pretrained claim-detection checkpoint.
    """

    #: env-pinned pretrained claim/check-worthiness model (#956, fetched by #959)
    PRETRAINED_MODEL_ENV = "NOESIS_CLAIM_MODEL"
    DEFAULT_PRETRAINED_MODEL = "Nithiwat/mdeberta-v3-base_claimbuster"

    def __init__(self, model_dir: Optional[Path] = None, pretrained: Optional[Any] = None) -> None:
        _reject_removed_backend("NOESIS_CLAIMS_BACKEND", "claim detection")
        self._model_dir = model_dir or _CLAIM_MODEL_DIR
        self._pipeline = None
        self._pretrained = pretrained          # (pipeline, model_name) or None
        self._try_load()
        if self._pipeline is None and self._pretrained is None:
            self._try_load_pretrained()

    def _try_load_pretrained(self) -> None:
        """Load the pinned ClaimBuster backend from the local model cache."""
        from src.argument_mining.model_registry import cached_model_path, resolved_pins

        pin = resolved_pins()["claim"]
        local_path = cached_model_path("claim")
        if local_path is None:
            raise RuntimeError(
                "pinned claim weights are not in the local cache; run `make models`"
            )
        from transformers import pipeline as hf_pipeline

        model_name = pin["model"]
        self._pretrained = (
            hf_pipeline(
                "text-classification",
                model=str(local_path),
                tokenizer=str(local_path),
                device=-1,
            ),
            model_name,
        )
        logger.info("ClaimDetector: pretrained backend active (%s)", model_name)

    @staticmethod
    def _pretrained_is_claim(label: str) -> bool:
        """Normalize label schemes across published claim-detection models.

        ClaimBuster-style checkpoints variously use LABEL_1, "claim",
        "checkworthy", "check-worthy factual sentence", or "cfs"."""
        text = label.lower()
        if text.startswith(("not", "non")) or text in ("label_0", "0", "nfs"):
            return False
        if text in ("label_1", "1", "cfs"):
            return True
        # The pinned ClaimBuster checkpoint is three-way: both "Unimportant
        # Factual" and "Check-worthy Factual" are claims for Noesis' binary
        # extraction task; only "Non-factual" is negative.
        return "factual" in text or "claim" in text or "check" in text

    def _try_load(self) -> None:
        if not (self._model_dir / "config.json").exists():
            logger.info(
                "ClaimDetector: no fine-tuned model at %s; trying pinned pretrained weights",
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
            logger.warning("ClaimDetector: fine-tuned model load failed", exc_info=True)


    @property
    def prediction_mode(self) -> str:
        """Return the active trained-model provenance."""
        if self._pipeline is not None:
            return f"model:{self._model_dir.name}"
        if self._pretrained is not None:
            return f"pretrained:{self._pretrained[1]}"
        raise RuntimeError("claim detector has no active model backend")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def predict(self, document: Document) -> List[ClaimPrediction]:
        """Return one ClaimPrediction per sentence in the document."""
        if document.source_type == "transcript" and document.content:
            from dataclasses import replace as dc_replace

            document = dc_replace(
                document, content=normalize_transcript_text(document.content)
            )
        sentences = sentences_from_document(document)
        if not sentences:
            return []
        if self._pipeline is not None:
            return self._predict_model(sentences)
        if self._pretrained is not None:
            return self._predict_pretrained(sentences)
        raise RuntimeError("claim detector has no active model backend")

    def _predict_pretrained(self, sentences: List[str]) -> List[ClaimPrediction]:
        pipeline_fn, _model_name = self._pretrained
        batch = pipeline_fn(sentences, truncation=True, max_length=128, batch_size=16)
        results = []
        for i, (sent, pred) in enumerate(zip(sentences, batch)):
            is_claim = self._pretrained_is_claim(str(pred["label"]))
            results.append(ClaimPrediction(
                text=sent,
                sentence_idx=i,
                is_claim=is_claim,
                confidence=round(float(pred["score"]), 4),
            ))
        return results

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


_TRANSCRIPT_TIMESTAMP = re.compile(r"[\[\(]?\b\d{1,2}:\d{2}(?::\d{2})?\b[\]\)]?")
_TRANSCRIPT_SPEAKER = re.compile(r"^[A-Z][A-Za-z .'-]{0,30}:\s+", re.MULTILINE)
_TRANSCRIPT_DISFLUENCY = re.compile(
    r"\b(?:um+|uh+|erm+|you know|i mean),?\s+|\blike,\s+", re.IGNORECASE
)


def normalize_transcript_text(text: str) -> str:
    """Normalize transcript artifacts before sentence-level claim detection.

    Timestamps and speaker tags are stripped, common disfluencies removed,
    and single line breaks (timestamped chunking mid-sentence) rejoined so
    fragmented sentences reassemble — the dominant transcript failure mode
    behind the recall gap (#956).
    """
    text = _TRANSCRIPT_TIMESTAMP.sub(" ", text)
    text = _TRANSCRIPT_SPEAKER.sub("", text)
    text = _TRANSCRIPT_DISFLUENCY.sub("", text)
    # Rejoin single newlines (chunking), keep paragraph breaks.
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


# ---------------------------------------------------------------------------
# Stance Classifier
# ---------------------------------------------------------------------------

class StanceClassifier:
    """4-class stance classifier: supportive / critical / neutral / ambiguous.

    Loads a fine-tuned ``distilbert-base-uncased`` checkpoint from
    ``models/stance_classifier/`` when available, otherwise uses the pinned
    pretrained NLI checkpoint.
    """

    #: hypothesis templates per stance class for the zero-shot NLI backend
    NLI_TEMPLATES = {
        "supportive": "This text is supportive of {topic}.",
        "critical": "This text is critical of {topic}.",
        "neutral": "This text reports on {topic} neutrally.",
        "ambiguous": "This text is ambiguous about {topic}.",
    }
    #: below this best-entailment score the NLI backend answers neutral
    NLI_FLOOR = 0.40

    def __init__(self, model_dir: Optional[Path] = None, nli: Optional[Any] = None) -> None:
        _reject_removed_backend("NOESIS_STANCE_BACKEND", "stance classification")
        self._model_dir = model_dir or _STANCE_MODEL_DIR
        self._pipeline = None
        self._nli = nli
        self._nli_cache: dict = {}
        self._try_load()
        if self._pipeline is None and self._nli is None:
            self._try_load_nli()

    def _try_load_nli(self) -> None:
        """Use pinned zero-shot NLI by default when its weights are cached."""
        from src.kb.nli import TransformersNLI

        self._nli = TransformersNLI()
        logger.info("StanceClassifier: zero-shot NLI backend active (%s)",
                    self._nli.model_name)

    def _try_load(self) -> None:
        if not (self._model_dir / "config.json").exists():
            logger.info(
                "StanceClassifier: no fine-tuned model at %s; trying pinned NLI weights",
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
            logger.warning("StanceClassifier: fine-tuned model load failed", exc_info=True)


    @property
    def prediction_mode(self) -> str:
        """Return the active trained-model provenance."""
        if self._pipeline is not None:
            return f"model:{self._model_dir.name}"
        if self._nli is not None:
            return self._nli.prediction_mode
        raise RuntimeError("stance classifier has no active model backend")

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
        if self._nli is not None:
            return self._predict_nli(sentences, topic)
        raise RuntimeError("stance classifier has no active model backend")

    def _predict_nli(self, sentences: List[str], topic: str) -> List[StancePrediction]:
        """Stance via entailment: one hypothesis per class, argmax wins.

        Scores are normalized across the four class hypotheses so the
        confidence reflects the margin between stances rather than a raw
        entailment probability; results are cached per (sentence, topic).
        """
        labels = list(self.NLI_TEMPLATES)
        pairs = [
            (sentence, self.NLI_TEMPLATES[stance].format(topic=topic))
            for sentence in sentences for stance in labels
        ]
        batch_scores = (
            self._nli.entailment_scores(pairs)
            if hasattr(self._nli, "entailment_scores") else None
        )
        results = []
        for i, sentence in enumerate(sentences):
            key = (sentence, topic)
            if key not in self._nli_cache:
                scores = {}
                for offset, stance in enumerate(labels):
                    if batch_scores is not None:
                        scores[stance] = batch_scores[i * len(labels) + offset]
                    else:
                        hypothesis = self.NLI_TEMPLATES[stance].format(topic=topic)
                        outcome = self._nli.classify(sentence, hypothesis)
                        scores[stance] = (
                            outcome.confidence if outcome.label == "entailment" else 0.0
                        )
                best = max(scores, key=scores.get)
                total = sum(scores.values())
                best_share = scores[best] / total if total else 0.0
                if (scores[best] < self.NLI_FLOOR or total <= 0
                        or best_share < 0.30):
                    self._nli_cache[key] = ("neutral", 0.5)
                else:
                    self._nli_cache[key] = (
                        best, round(best_share, 4)
                    )
            stance, confidence = self._nli_cache[key]
            results.append(
                StancePrediction(
                    text=sentence, sentence_idx=i, topic=topic,
                    stance=stance, confidence=confidence,
                )
            )
        return results

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
