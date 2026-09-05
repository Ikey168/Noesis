"""
Shared NLI (natural-language inference) backend.

One pinned pretrained NLI model serves three surfaces: stance classification,
frame classification, and claim-pair relation linking. This module provides
the common interface and its trained-model implementation:

- :class:`TransformersNLI` — the pinned pretrained cross-encoder (lazy
  import; requires ``transformers``/``torch`` and a fetched model). Model
  name comes from ``NOESIS_NLI_MODEL``.
Every result carries ``prediction_mode`` for downstream honesty reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ENTAILMENT = "entailment"
CONTRADICTION = "contradiction"
NEUTRAL = "neutral"

#: default pinned model; #959 wires revision pinning + fetch
DEFAULT_NLI_MODEL = "cross-encoder/nli-deberta-v3-base"
NLI_MODEL_ENV = "NOESIS_NLI_MODEL"

@dataclass
class NLIResult:
    label: str          # entailment | contradiction | neutral
    confidence: float   # model score [0, 1], not a calibration guarantee
    prediction_mode: str


class TransformersNLI:
    """Pinned pretrained NLI cross-encoder, lazily loaded.

    Raises ``RuntimeError`` from the constructor when the stack or cached
    weights are unavailable.
    """

    def __init__(self, model_name: Optional[str] = None, *, evaluation_model: bool = False) -> None:
        from src.argument_mining.model_registry import cached_model_path, resolved_pins

        pin = resolved_pins()["nli"]
        self.model_name = model_name or pin["model"]
        local_path = cached_model_path("nli")
        if evaluation_model:
            if model_name != "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli":
                raise ValueError("unsupported evaluation NLI model")
            from src.integrations.models import model_path
            local_path = model_path(model_name)
        if local_path is None or not evaluation_model and self.model_name != pin["model"]:
            raise RuntimeError(
                "pinned NLI weights are not in the local cache; run `make models`"
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

        self._tokenizer = AutoTokenizer.from_pretrained(
            str(local_path), local_files_only=True
        )
        self._model = AutoModelForSequenceClassification.from_pretrained(
            str(local_path), local_files_only=True
        )
        self._model.eval()
        # id2label varies across NLI checkpoints; normalize to our labels.
        self._id2label = {
            index: label.lower()
            for index, label in self._model.config.id2label.items()
        }
        self._entailment_index = next(
            (index for index, label in self._id2label.items()
             if label == ENTAILMENT),
            None,
        )
        if self._entailment_index is None:
            raise RuntimeError(
                f"NLI model {self.model_name!r} has no entailment output label"
            )
        self._contradiction_index = next(
            (index for index, label in self._id2label.items()
             if label == CONTRADICTION),
            None,
        )
        if self._contradiction_index is None:
            raise RuntimeError(
                f"NLI model {self.model_name!r} has no contradiction output label"
            )
        self.name = f"nli:{self.model_name}"
        self.prediction_mode = f"zero-shot:{self.model_name}"
        self.model_version = self.model_name

    def classify(self, premise: str, hypothesis: str) -> NLIResult:  # pragma: no cover - needs model
        import torch

        inputs = self._bounded_inputs([premise], [hypothesis])
        with torch.no_grad():
            logits = self._model(**inputs).logits[0]
        probs = torch.softmax(logits, dim=-1).tolist()
        best = max(range(len(probs)), key=lambda index: probs[index])
        label = self._id2label.get(best, NEUTRAL)
        if label not in (ENTAILMENT, CONTRADICTION, NEUTRAL):
            label = NEUTRAL
        return NLIResult(label, round(float(probs[best]), 4), self.prediction_mode)

    def _bounded_inputs(self, premises, hypotheses):
        if any(len(text)>262144 for text in [*premises,*hypotheses]):
            raise ValueError('NLI input exceeds the bounded text limit')
        inputs=self._tokenizer(premises,hypotheses,return_tensors='pt',padding=True,truncation=False)
        if inputs['input_ids'].shape[-1]>512:
            raise ValueError('NLI pair exceeds 512 tokens; use classify_evidence for complete span coverage')
        return inputs

    def classify_evidence(self, premise, hypothesis, *, overlap_tokens=32, max_windows=64):
        """Window-level full-span assessment; never silently discard a tail."""
        from src.kb.nli_evidence import classify_evidence
        return classify_evidence(self,premise,hypothesis,overlap_tokens=overlap_tokens,max_windows=max_windows)

    def entailment_scores(
        self, pairs: list[tuple[str, str]], *, batch_size: int = 32
    ) -> list[float]:  # pragma: no cover - needs model
        """Return entailment probabilities for premise/hypothesis pairs.

        Zero-shot classification must compare the entailment logit for every
        candidate label, even when neutral is the argmax for each individual
        pair. Batching also keeps full benchmark runs practical on CPU.
        """
        import torch

        scores: list[float] = []
        for start in range(0, len(pairs), max(1, batch_size)):
            batch = pairs[start:start + max(1, batch_size)]
            inputs = self._bounded_inputs([premise for premise, _ in batch], [hypothesis for _, hypothesis in batch])
            with torch.no_grad():
                logits = self._model(**inputs).logits
            # Match transformers' multi-label zero-shot semantics: neutral is
            # excluded and entailment competes directly with contradiction.
            binary = logits[:, [self._contradiction_index, self._entailment_index]]
            probabilities = torch.softmax(binary, dim=-1)
            scores.extend(
                round(float(row[1]), 6)
                for row in probabilities
            )
        return scores

    def entailment_score(self, premise: str, hypothesis: str) -> float:
        return self.entailment_scores([(premise, hypothesis)], batch_size=1)[0]


def get_nli_backend(model_name: Optional[str] = None):
    """Return the pinned pretrained backend, failing closed when unavailable."""
    return TransformersNLI(model_name)
