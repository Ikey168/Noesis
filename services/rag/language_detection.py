"""Optional language-detection backends with explicit uncertainty and offsets."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any


def _segments(text: str) -> list[tuple[int, int, str]]:
    """Return non-empty sentence/line-like spans over the exact input string."""
    output: list[tuple[int, int, str]] = []
    start = 0
    for match in re.finditer(r"(?:[.!?](?=\s|$)|\n+)", text):
        end = match.end()
        raw = text[start:end]
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        if right > left:
            output.append((start + left, start + right, raw[left:right]))
        start = end
    raw = text[start:]
    left = len(raw) - len(raw.lstrip())
    right = len(raw.rstrip())
    if right > left:
        output.append((start + left, start + right, raw[left:right]))
    return output


@lru_cache(maxsize=8)
def _lingua_detector(language_codes: tuple[str, ...]):
    try:
        from lingua import Language, LanguageDetectorBuilder
    except ImportError as exc:  # pragma: no cover - optional dependency boundary
        raise RuntimeError("install lingua-language-detector for the Lingua backend") from exc
    mapping = {
        "de": Language.GERMAN,
        "en": Language.ENGLISH,
        "fr": Language.FRENCH,
        "es": Language.SPANISH,
        "it": Language.ITALIAN,
        "nl": Language.DUTCH,
        "pl": Language.POLISH,
    }
    try:
        languages = [mapping[code] for code in language_codes]
    except KeyError as exc:
        raise ValueError(f"unsupported Lingua language code: {exc.args[0]}") from exc
    if len(languages) < 2:
        raise ValueError("Lingua evaluation requires at least two configured languages")
    return LanguageDetectorBuilder.from_languages(*languages).build()


def detect_with_lingua(
    text: str,
    *,
    languages: tuple[str, ...] = ("de", "en"),
    confidence_threshold: float = 0.75,
    detect_segments: bool = True,
) -> dict[str, Any]:
    if not 0.0 <= confidence_threshold <= 1.0:
        raise ValueError("language confidence threshold must be between zero and one")
    if not text.strip():
        return {
            "backend": "lingua",
            "language": "unknown",
            "confidence": None,
            "uncertain": True,
            "segments": [],
            "coordinate_system": "input-char-offset-v1",
        }
    detector = _lingua_detector(tuple(languages))

    def classify(value: str) -> tuple[str, float, bool]:
        scores = detector.compute_language_confidence_values(value)
        if not scores:
            return "unknown", 0.0, True
        best = scores[0]
        code = best.language.iso_code_639_1.name.casefold()
        confidence = float(best.value)
        uncertain = confidence < confidence_threshold
        return ("unknown" if uncertain else code, confidence, uncertain)

    language, confidence, uncertain = classify(text)
    segments = []
    if detect_segments:
        for start, end, value in _segments(text):
            segment_language, segment_confidence, segment_uncertain = classify(value)
            segments.append(
                {
                    "start": start,
                    "end": end,
                    "text": value,
                    "language": segment_language,
                    "confidence": segment_confidence,
                    "uncertain": segment_uncertain,
                }
            )
    return {
        "backend": "lingua",
        "language": language,
        "confidence": confidence,
        "uncertain": uncertain,
        "configured_languages": list(languages),
        "confidence_threshold": confidence_threshold,
        "segments": segments,
        "coordinate_system": "input-char-offset-v1",
    }
