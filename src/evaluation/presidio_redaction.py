"""Optional Presidio detection boundary for derived, traceable redactions."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from src.evaluation.runtime_errors import BackendError
from src.evaluation.workflow_review import redacted_artifact


@dataclass(frozen=True)
class PresidioConfiguration:
    languages: tuple[str, ...] = ("de", "en")
    entities: tuple[str, ...] = (
        "PERSON",
        "LOCATION",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "IBAN_CODE",
    )
    score_threshold: float = 0.5
    policy_version: str = "presidio-derived-redaction-v1"


class PresidioRedactor:
    """Run Presidio locally and map detections to Noesis derived artifacts."""

    def __init__(
        self, analyzer: Any, configuration: PresidioConfiguration | None = None
    ):
        self.analyzer = analyzer
        self.configuration = configuration or PresidioConfiguration()
        config = self.configuration
        if (
            not config.languages
            or not set(config.languages) <= {"de", "en"}
            or not config.entities
        ):
            raise ValueError("explicit supported languages and entity types required")
        if (
            not math.isfinite(config.score_threshold)
            or not 0 <= config.score_threshold <= 1
            or not config.policy_version
        ):
            raise ValueError("invalid redaction score threshold or policy version")

    @classmethod
    def from_spacy_models(
        cls,
        *,
        german_model: str = "de_core_news_sm",
        english_model: str = "en_core_web_sm",
        configuration: PresidioConfiguration | None = None,
    ) -> PresidioRedactor:
        try:
            import spacy
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import SpacyNlpEngine
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise BackendError(
                "optional_dependency_unavailable",
                "install the optional Presidio and spaCy dependencies",
            ) from exc
        config = configuration or PresidioConfiguration()
        model_by_language = {"de": german_model, "en": english_model}
        models = [
            {"lang_code": language, "model_name": model_by_language[language]}
            for language in config.languages
            if language in model_by_language
        ]
        if len(models) != len(config.languages):
            raise ValueError(
                "a pinned spaCy model is required for every configured language"
            )

        class LocalOnlySpacyEngine(SpacyNlpEngine):
            def load(self):
                # The native provider downloads absent models. Replace that load
                # path, rather than race a preflight check against a later loader.
                loaded = {}
                for spec in self.models:
                    try:
                        loaded[spec["lang_code"]] = spacy.load(spec["model_name"])
                    except (OSError, ImportError) as exc:
                        raise BackendError(
                            "model_unavailable",
                            "install configured spaCy models explicitly before inference",
                        ) from exc
                self.nlp = loaded

        engine = LocalOnlySpacyEngine(models=models)
        engine.load()
        analyzer = AnalyzerEngine(
            nlp_engine=engine,
            supported_languages=list(config.languages),
            default_score_threshold=config.score_threshold,
        )
        return cls(analyzer, config)

    def detect(self, text: str, *, language: str) -> list[dict[str, Any]]:
        if language not in self.configuration.languages:
            raise BackendError(
                "unsupported_language", "no configured recognizer for this language"
            )
        if not isinstance(text, str) or len(text) > 262144:
            raise BackendError("input_limit", "redaction requires bounded source text")
        results = self.analyzer.analyze(
            text=text,
            language=language,
            entities=list(self.configuration.entities),
            score_threshold=self.configuration.score_threshold,
        )
        output = []
        for result in results:
            if (
                len(output) >= 10000
                or str(result.entity_type) not in self.configuration.entities
            ):
                raise BackendError(
                    "invalid_model_output",
                    "unexpected redaction labels or result count",
                )
            if (
                not math.isfinite(float(result.score))
                or not 0 <= float(result.score) <= 1
            ):
                raise BackendError(
                    "invalid_model_output", "invalid redaction confidence"
                )
            start, end = int(result.start), int(result.end)
            if not 0 <= start < end <= len(text):
                raise ValueError("Presidio returned a span outside source text")
            output.append(
                {
                    "start": start,
                    "end": end,
                    "entity_type": str(result.entity_type),
                    "score": float(result.score),
                    "text": text[start:end],
                    "recognition_metadata": dict(
                        getattr(result, "recognition_metadata", None) or {}
                    ),
                }
            )
        return sorted(
            output, key=lambda row: (row["start"], row["end"], row["entity_type"])
        )

    def redact(self, original: dict[str, Any], *, language: str) -> dict[str, Any]:
        text = str(original.get("text", ""))
        detections = self.detect(text, language=language)
        # Overlapping recognizers can produce the same span; use the highest-score
        # detection per exact span and reject partially overlapping ambiguity.
        by_span: dict[tuple[int, int], dict[str, Any]] = {}
        for detection in detections:
            key = (detection["start"], detection["end"])
            current = by_span.get(key)
            if current is None or detection["score"] > current["score"]:
                by_span[key] = detection
        selected = sorted(by_span.values(), key=lambda row: (row["start"], row["end"]))
        non_overlapping = []
        for detection in selected:
            if non_overlapping and detection["start"] < non_overlapping[-1]["end"]:
                # Redact the union; selecting only the most confident detection
                # can expose the uncovered tail of another sensitive region.
                previous = non_overlapping[-1]
                previous["end"] = max(previous["end"], detection["end"])
                previous["score"] = max(previous["score"], detection["score"])
                previous["entity_type"] = "SENSITIVE"
                continue
            non_overlapping.append(detection)
        artifact = redacted_artifact(
            original,
            non_overlapping,
            policy_version=self.configuration.policy_version,
        )
        return {
            **artifact,
            "detector": "presidio-analyzer",
            "language": language,
            "detection_count": len(non_overlapping),
            "detector_scores": [
                {
                    "start": detection["start"],
                    "end": detection["end"],
                    "entity_type": detection["entity_type"],
                    "score": detection["score"],
                }
                for detection in non_overlapping
            ],
        }


def exact_span_metrics(
    expected: Iterable[tuple[int, int, str]], detected: Iterable[dict[str, Any]]
) -> dict[str, float | int]:
    truth = {
        (int(start), int(end), str(entity_type)) for start, end, entity_type in expected
    }
    observed = {
        (int(row["start"]), int(row["end"]), str(row["entity_type"]))
        for row in detected
    }
    tp = len(truth & observed)
    fp = len(observed - truth)
    fn = len(truth - observed)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0,
    }
