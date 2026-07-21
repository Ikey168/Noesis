"""
Shared NLI (natural-language inference) backend.

One pinned pretrained NLI model serves three surfaces: stance classification,
frame classification, and claim-pair relation linking. This module provides
the common interface plus two implementations:

- :class:`TransformersNLI` — the pinned pretrained cross-encoder (lazy
  import; requires ``transformers``/``torch`` and a fetched model). Model
  name comes from ``NOESIS_NLI_MODEL``.
- :class:`HeuristicNLI` — the fully-offline floor: lexical overlap plus
  negation/antonym cues. Degraded quality, still valid output, consistent
  with the repo-wide fallback discipline.

Every result carries ``prediction_mode`` so downstream honesty reporting
(#958) can distinguish model-grade from heuristic-grade output.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

ENTAILMENT = "entailment"
CONTRADICTION = "contradiction"
NEUTRAL = "neutral"

#: default pinned model; #959 wires revision pinning + fetch
DEFAULT_NLI_MODEL = "cross-encoder/nli-deberta-v3-base"
NLI_MODEL_ENV = "NOESIS_NLI_MODEL"

_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "is", "are",
    "was", "were", "be", "been", "this", "that", "these", "those", "it", "its",
    "with", "as", "by", "at", "from", "their", "our", "we", "has", "have",
    "had", "will", "would", "said", "says", "say",
}

_NEGATIONS = {
    "not", "no", "never", "n't", "denies", "denied", "deny", "refutes",
    "refuted", "rejects", "rejected", "disputes", "disputed", "without",
}

#: cheap directional antonyms common in news/economics claims
_ANTONYM_PAIRS = [
    ("rise", "fall"), ("rises", "falls"), ("rose", "fell"),
    ("increase", "decrease"), ("increased", "decreased"),
    ("up", "down"), ("gain", "loss"), ("gains", "losses"),
    ("higher", "lower"), ("approve", "reject"), ("approved", "rejected"),
    ("growth", "contraction"), ("surge", "plunge"), ("won", "lost"),
    ("confirmed", "denied"), ("supports", "opposes"), ("more", "fewer"),
]


@dataclass
class NLIResult:
    label: str          # entailment | contradiction | neutral
    confidence: float   # calibrated-ish [0, 1]
    prediction_mode: str


def _content_tokens(text: str) -> Set[str]:
    tokens = re.findall(r"[a-z0-9']+", text.lower())
    return {token for token in tokens if token not in _STOPWORDS}


def _negation_count(text: str) -> int:
    tokens = re.findall(r"[a-z']+", text.lower())
    return sum(1 for token in tokens if token in _NEGATIONS)


class HeuristicNLI:
    """Offline floor: overlap + negation parity + directional antonyms."""

    name = "heuristic"
    prediction_mode = "heuristic"
    model_version = "heuristic-v1"

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        premise_tokens = _content_tokens(premise)
        hypothesis_tokens = _content_tokens(hypothesis)
        if not premise_tokens or not hypothesis_tokens:
            return NLIResult(NEUTRAL, 0.3, self.prediction_mode)

        overlap = len(premise_tokens & hypothesis_tokens) / len(
            premise_tokens | hypothesis_tokens
        )
        negation_flip = (_negation_count(premise) % 2) != (
            _negation_count(hypothesis) % 2
        )
        antonym = any(
            (a in premise_tokens and b in hypothesis_tokens)
            or (b in premise_tokens and a in hypothesis_tokens)
            for a, b in _ANTONYM_PAIRS
        )

        if overlap >= 0.4 and (negation_flip or antonym):
            return NLIResult(
                CONTRADICTION,
                round(min(0.85, 0.5 + overlap / 2), 3),
                self.prediction_mode,
            )
        if overlap >= 0.6 and not negation_flip and not antonym:
            return NLIResult(
                ENTAILMENT,
                round(min(0.85, 0.4 + overlap / 2), 3),
                self.prediction_mode,
            )
        return NLIResult(NEUTRAL, round(0.4 + (1 - overlap) / 4, 3), self.prediction_mode)


class TransformersNLI:
    """Pinned pretrained NLI cross-encoder, lazily loaded.

    Raises ``RuntimeError`` from the constructor when the stack is not
    available — callers should use :func:`get_nli_backend`, which falls back
    to :class:`HeuristicNLI`.
    """

    def __init__(self, model_name: Optional[str] = None) -> None:
        self.model_name = model_name or os.environ.get(
            NLI_MODEL_ENV, DEFAULT_NLI_MODEL
        )
        try:
            from transformers import (  # noqa: F401
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )
            import torch  # noqa: F401
        except Exception as exc:  # pragma: no cover - env-dependent
            raise RuntimeError(f"transformers/torch unavailable: {exc}") from exc

        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        self._model = AutoModelForSequenceClassification.from_pretrained(
            self.model_name
        )
        self._model.eval()
        # id2label varies across NLI checkpoints; normalize to our labels.
        self._id2label = {
            index: label.lower()
            for index, label in self._model.config.id2label.items()
        }
        self.name = f"nli:{self.model_name}"
        self.prediction_mode = f"zero-shot:{self.model_name}"
        self.model_version = self.model_name

    def classify(self, premise: str, hypothesis: str) -> NLIResult:  # pragma: no cover - needs model
        import torch

        inputs = self._tokenizer(
            premise, hypothesis, return_tensors="pt", truncation=True, max_length=512
        )
        with torch.no_grad():
            logits = self._model(**inputs).logits[0]
        probs = torch.softmax(logits, dim=-1).tolist()
        best = max(range(len(probs)), key=lambda index: probs[index])
        label = self._id2label.get(best, NEUTRAL)
        if label not in (ENTAILMENT, CONTRADICTION, NEUTRAL):
            label = NEUTRAL
        return NLIResult(label, round(float(probs[best]), 4), self.prediction_mode)


def get_nli_backend(model_name: Optional[str] = None):
    """The pinned pretrained backend when available, else the heuristic floor."""
    try:
        return TransformersNLI(model_name)
    except Exception:
        return HeuristicNLI()
