import pytest

pytest.importorskip("bs4")

from services.rag.language_detection import detect_with_lingua
from services.rag.normalization import ArticleNormalizer


def test_lingua_optional_backend_reports_uncertainty_when_dependency_missing_or_short():
    try:
        result = detect_with_lingua("Berlin", confidence_threshold=0.99)
    except RuntimeError as exc:
        assert "lingua-language-detector" in str(exc)
        return
    assert result["language"] == "unknown"
    assert result["uncertain"] is True


def test_lingua_segments_preserve_exact_offsets_when_available():
    pytest.importorskip("lingua")
    text = "Die Behörde veröffentlicht neue Daten. The English abstract follows."
    result = detect_with_lingua(text, confidence_threshold=0.6)
    assert len(result["segments"]) == 2
    for segment in result["segments"]:
        assert text[segment["start"] : segment["end"]] == segment["text"]
    assert {segment["language"] for segment in result["segments"]} == {"de", "en"}


def test_article_normalizer_exposes_lingua_detection_without_changing_default():
    pytest.importorskip("lingua")
    normalizer = ArticleNormalizer(language_backend="lingua", min_paragraph_length=1)
    result = normalizer.normalize_article(
        "Die Berliner Behörde veröffentlicht einen ausführlichen deutschen Bericht."
    )
    assert result["language"] == "de"
    assert result["language_detection"]["backend"] == "lingua"
    assert result["language_detection"]["coordinate_system"] == "input-char-offset-v1"
    assert ArticleNormalizer().language_backend == "langdetect"
