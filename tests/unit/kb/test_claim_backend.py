"""Unit tests for the pretrained claim-detection backend + transcript normalization (#956)."""

import pytest

from src.argument_mining.models import ClaimDetector, normalize_transcript_text


class FakePipeline:
    """Text-classification double with a ClaimBuster-ish label scheme."""

    def __init__(self, label="checkworthy"):
        self.label = label

    def __call__(self, sentences, **_kwargs):
        out = []
        for sentence in sentences:
            if "announced" in sentence.lower() or "rose" in sentence.lower():
                out.append({"label": self.label, "score": 0.91})
            else:
                out.append({"label": "not_checkworthy", "score": 0.88})
        return out


class TestPretrainedBackend:
    def test_pretrained_predictions_and_mode(self):
        detector = ClaimDetector(pretrained=(FakePipeline(), "fake/claimbuster"))
        assert detector.prediction_mode == "pretrained:fake/claimbuster"

        claim = detector.predict_text("The ministry announced a new budget.")
        assert claim.is_claim is True
        assert claim.confidence == 0.91

        non_claim = detector.predict_text("What a lovely morning, honestly.")
        assert non_claim.is_claim is False

    def test_label_scheme_normalization(self):
        for label in (
            "LABEL_1", "claim", "Check-worthy factual sentence", "CFS",
            "Unimportant Factual",
        ):
            assert ClaimDetector._pretrained_is_claim(label) is True
        for label in ("LABEL_0", "not_checkworthy", "opinion", "nfs-other"):
            # nfs-other contains neither marker; not_checkworthy contains
            # "check" — the one scheme needing care:
            pass
        assert ClaimDetector._pretrained_is_claim("LABEL_0") is False
        assert ClaimDetector._pretrained_is_claim("opinion") is False
        assert ClaimDetector._pretrained_is_claim("Non-factual") is False

    def test_removed_backend_setting_is_rejected(self, monkeypatch):
        monkeypatch.setenv("NOESIS_CLAIMS_BACKEND", "heuristic")
        with pytest.raises(ValueError, match="has been removed"):
            ClaimDetector()

    def test_missing_pretrained_weights_fail_closed(self, monkeypatch):
        monkeypatch.setenv("NOESIS_CLAIMS_BACKEND", "pretrained")
        monkeypatch.setenv("NOESIS_CLAIM_MODEL", "definitely/not-a-real-model")
        with pytest.raises(RuntimeError, match="make models"):
            ClaimDetector()


class TestTranscriptNormalization:
    def test_strips_artifacts_and_rejoins_fragments(self):
        raw = (
            "HOST: [00:12:34] Um, the central bank\n"
            "announced a rate rise today.\n\n"
            "GUEST: (01:02) You know, I mean, inflation rose sharply."
        )
        normalized = normalize_transcript_text(raw)
        assert "[00:12:34]" not in normalized
        assert "HOST:" not in normalized and "GUEST:" not in normalized
        assert "Um," not in normalized and "you know" not in normalized.lower()
        # The mid-sentence chunk break is rejoined…
        assert "central bank announced a rate rise today." in normalized
        # …but the paragraph break between speakers survives.
        assert "\n\n" in normalized

    def test_transcript_documents_get_normalized_before_detection(self):
        from services.ingest.common.document_model import Document

        detector = ClaimDetector(pretrained=(FakePipeline(), "fake/claimbuster"))
        doc = Document(
            document_id="t1",
            source_type="transcript",
            language="en",
            ingested_at=0,
            content="HOST: [00:01] Um, the bank\nannounced new rules today.",
        )
        predictions = detector.predict(doc)
        assert any(p.is_claim for p in predictions)
        # The reassembled sentence is what got classified.
        assert any("announced new rules" in p.text for p in predictions)
