import copy
import hashlib

import numpy as np
import pytest

from src.evaluation.media_backends import (
    PaddleOCRBackend,
    WhisperXAligner,
    render_pages,
)
from src.evaluation.runtime_errors import BackendError


def test_pdf_render_hash_page_bounds_and_paddle_native_coordinates():
    from pathlib import Path

    path = Path("tests/fixtures/pdf_benchmark/digital.pdf")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    class Engine:
        def predict(self, image, **kwargs):
            assert image.shape[2] == 3 and kwargs["text_rec_score_thresh"] == 0
            yield {
                "res": {
                    "rec_texts": ["Müller", ""],
                    "rec_scores": [0.95, 0.2],
                    "rec_boxes": [[1, 1, 20, 20], [20, 20, 30, 30]],
                    "dt_polys": [0, 1, 2],
                }
            }

    pages = render_pages(path, digest, dpi=72)
    image, receipt = next(pages)
    result = PaddleOCRBackend(engine=Engine()).recognize(image, receipt=receipt)
    assert result["unrecognized_detection_count"] == 1
    assert result["regions"][1]["uncertain"]
    assert receipt["page"] == 1 and len(receipt["render_sha256"]) == 64
    pages.close()
    with pytest.raises(BackendError, match="differs"):
        list(render_pages(path, "0" * 64))
    with pytest.raises(BackendError, match="pixel"):
        list(render_pages(path, digest, max_pixels=10))
    with pytest.raises(ValueError):
        list(render_pages(path, digest, pages=[999]))


def test_alignment_preserves_speakers_and_does_not_publish_interpolated_word_times():
    original = {
        "media_id": "m",
        "segments": [{"start": 0.0, "end": 1.0, "text": "Hallo 2026", "speaker": "s1"}],
    }
    before = copy.deepcopy(original)

    def aligner(segments, *args, **kwargs):
        assert kwargs["return_char_alignments"]
        return {
            "word_segments": [
                {"word": "Hallo", "start": 0.1, "end": 0.4, "score": 0.9},
                {"word": "2026", "start": 0.4, "end": 0.8},
            ]
        }

    result = WhisperXAligner("de", aligner=aligner).align(
        np.zeros(16000), original, media_sha256="a" * 64
    )
    assert result["coverage"] == 0.5 and result["unaligned_words"][0]["start_s"] is None
    assert result["aligned_words"][0]["speaker"] == "s1"
    assert original == before and result["original_segments"] == before["segments"]
    with pytest.raises(BackendError, match="only"):
        WhisperXAligner("xx", aligner=aligner)


def test_alignment_rejects_nonfinite_audio_and_token_changes():
    transcript = {
        "media_id": "m",
        "segments": [{"start": 0.0, "end": 1.0, "text": "Hallo"}],
    }
    bad = lambda *a, **k: {
        "word_segments": [{"word": "Invented", "start": 0.0, "end": 1.0, "score": 0.9}]
    }
    backend = WhisperXAligner("en", aligner=bad)
    with pytest.raises(ValueError):
        backend.align([float("nan")], transcript, media_sha256="a" * 64)
    with pytest.raises(BackendError, match="original"):
        backend.align(np.zeros(16000), transcript, media_sha256="a" * 64)


def test_pipe_only_audio_decode_is_bounded_and_rejects_external_playlists():
    import io
    import shutil
    import wave

    import pytest

    from src.evaluation.media_backends import decode_audio_bytes
    from src.evaluation.runtime_errors import BackendError

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    output = io.BytesIO()
    with wave.open(output, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(16000)
        clip.writeframes(b"\x00\x00" * 8000)
    assert len(decode_audio_bytes(output.getvalue(), max_seconds=1)) == 8000
    with pytest.raises(BackendError, match="duration"):
        decode_audio_bytes(output.getvalue(), max_seconds=0.1)
    with pytest.raises(BackendError, match="pipe-only"):
        decode_audio_bytes(b"#EXTM3U\n#EXTINF:1,\nhttps://127.0.0.1/private.wav\n")


def test_existing_tesseract_baseline_checks_source_and_page_limits_before_ocr(
    monkeypatch,
):
    from pathlib import Path

    from src.evaluation.media_backends import media_job
    from src.ingestion.connectors.book import pdf_parser

    path = Path("tests/fixtures/ocr_benchmark/authored-scans.pdf")
    payload = {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "max_pages": 1,
    }
    calls = []
    monkeypatch.setattr(pdf_parser, "_parse_with_ocr", lambda raw: calls.append(raw))
    with pytest.raises(BackendError, match="budget"):
        media_job("ocr-tesseract", payload)
    assert calls == []
    with pytest.raises(BackendError, match="differs"):
        media_job("ocr-tesseract", {**payload, "sha256": "0" * 64, "max_pages": 4})
    assert calls == []
    with pytest.raises(BackendError, match="no text"):
        media_job("ocr-tesseract", {**payload, "max_pages": 4})
    assert len(calls) == 1
