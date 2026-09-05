"""Optional language and sentence adapters retaining exact input offsets."""

from functools import lru_cache
from .common import IntegrationError, finite, version


@lru_cache(maxsize=16)
def _detector(languages):
    version("lingua-language-detector")
    from lingua import Language, LanguageDetectorBuilder

    try:
        selected = [
            Language.from_iso_code_639_1(
                getattr(__import__("lingua").IsoCode639_1, code.upper())
            )
            for code in languages
        ]
    except (AttributeError, ValueError) as exc:
        raise IntegrationError(
            "unsupported_language", "Use supported ISO 639-1 language codes"
        ) from exc
    if len(selected) < 2:
        raise IntegrationError(
            "invalid_input",
            "Language detection requires at least two candidate languages",
        )
    return LanguageDetectorBuilder.from_languages(*selected).build()


def detect_language(
    text, *, languages=("de", "en"), minimum_confidence=0.8, minimum_margin=0.2
):
    if not isinstance(text, str) or len(text) > 1_000_000:
        raise IntegrationError("invalid_input", "Language input must be bounded text")
    minimum_confidence = finite(minimum_confidence, "confidence", 0, 1)
    minimum_margin = finite(minimum_margin, "margin", 0, 1)
    languages = tuple(sorted(set(languages)))
    detector = _detector(languages)
    scores = detector.compute_language_confidence_values(text) if text.strip() else []
    confident = bool(
        scores
        and scores[0].value >= minimum_confidence
        and scores[0].value - (scores[1].value if len(scores) > 1 else 0)
        >= minimum_margin
    )
    segments = []
    for item in detector.detect_multiple_languages_of(text) if text.strip() else []:
        segments.append(
            {
                "start": item.start_index,
                "end": item.end_index,
                "language": item.language.iso_code_639_1.name.lower(),
            }
        )
    return {
        "language": scores[0].language.iso_code_639_1.name.lower()
        if confident
        else "unknown",
        "status": "detected" if confident else "uncertain",
        "segments": segments,
        "confidence": [
            {"language": s.language.iso_code_639_1.name.lower(), "value": s.value}
            for s in scores
        ],
        "producer": {
            "backend": "lingua",
            "version": version("lingua-language-detector"),
            "languages": list(languages),
        },
        "offset_basis": "input Unicode code points",
        "score_semantics": "relative language confidence, not calibrated correctness",
    }


class SaTSegmenter:
    def __init__(self, model_path):
        # A local, operator-pinned model directory prevents implicit model changes.
        from pathlib import Path

        if not Path(model_path).is_dir():
            raise IntegrationError(
                "model_unavailable", "SaT requires a local pinned model directory"
            )
        version("wtpsplit")
        from wtpsplit import SaT

        self.model = SaT(str(model_path))

    def __call__(self, text):
        if len(text) > 1_000_000:
            raise IntegrationError("input_limit", "Segmentation text is too large")
        sentences = self.model.split(text)
        spans, cursor = [], 0
        for sentence in sentences:
            # Only whitespace may be omitted; never fuzzy-align altered text.
            if not sentence:
                continue
            start = text.find(sentence, cursor)
            if start < 0 or text[cursor:start].strip():
                raise IntegrationError(
                    "offset_mismatch", "Segmenter changed or omitted source text"
                )
            end = start + len(sentence)
            spans.append((start, end))
            cursor = end
        if text[cursor:].strip():
            raise IntegrationError(
                "offset_mismatch", "Segmenter omitted trailing source text"
            )
        return spans
