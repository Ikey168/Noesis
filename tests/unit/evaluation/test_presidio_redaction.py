from dataclasses import dataclass

from src.evaluation.presidio_redaction import PresidioRedactor, exact_span_metrics


@dataclass
class Result:
    entity_type: str
    start: int
    end: int
    score: float
    recognition_metadata: dict | None = None


class Analyzer:
    def analyze(self, **kwargs):
        text = kwargs["text"]
        start = text.index("Max Müller")
        return [Result("PERSON", start, start + len("Max Müller"), 0.9)]


def test_redactor_preserves_original_and_does_not_leak_removed_text():
    original = {
        "source_id": "doc",
        "source_revision": "r1",
        "text": "Kontakt Max Müller in Berlin",
    }
    before = dict(original)
    artifact = PresidioRedactor(Analyzer()).redact(original, language="de")
    assert original == before
    assert "Max Müller" not in artifact["text"]
    assert artifact["original_embedded"] is False
    assert artifact["decisions"][0]["locator_id"]
    assert "source_text_sha256" not in artifact["decisions"][0]
    assert "Max Müller" not in str(artifact["detector_scores"])


def test_exact_span_metrics_count_false_positive_and_false_negative():
    expected = [(0, 4, "PERSON"), (10, 15, "LOCATION")]
    detected = [
        {"start": 0, "end": 4, "entity_type": "PERSON"},
        {"start": 20, "end": 25, "entity_type": "LOCATION"},
    ]
    metrics = exact_span_metrics(expected, detected)
    assert metrics["true_positives"] == 1
    assert metrics["false_positives"] == 1
    assert metrics["false_negatives"] == 1


def test_missing_spacy_models_never_trigger_presidio_download(monkeypatch):
    import pytest

    spacy = pytest.importorskip("spacy")
    pytest.importorskip("presidio_analyzer")
    from src.evaluation.runtime_errors import BackendError

    calls = []

    def forbidden(*a, **k):
        calls.append(True)
        raise AssertionError("implicit model download attempted")

    monkeypatch.setattr(spacy.cli, "download", forbidden)
    with pytest.raises(BackendError) as error:
        PresidioRedactor.from_spacy_models(
            german_model="noesis_nonexistent_spacy_model",
            english_model="noesis_nonexistent_spacy_model",
        )
    assert error.value.code == "model_unavailable" and calls == []


def test_redaction_metadata_does_not_copy_sensitive_source_identifiers():
    import json

    original = {
        "source_id": "notes/Max Müller/contact",
        "source_revision": "Max Müller revision 1",
        "text": "Kontakt Max Müller in Berlin",
    }
    artifact = PresidioRedactor(Analyzer()).redact(original, language="de")
    assert "Max Müller" not in json.dumps(artifact, ensure_ascii=False)
    assert (
        artifact["source_reference"]["resolution"] == "authorized-artifact-dependencies"
    )
    assert "Max Müller" in original["source_id"]
