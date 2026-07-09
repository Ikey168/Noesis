"""Unit tests for video keyframes + on-screen OCR (#782)."""

from __future__ import annotations

from services.ingest.common.document_model import Document
from src.ingestion.connectors.media.keyframes import (
    Keyframe,
    keyframe_documents,
    media_fragment,
    ocr_keyframes,
)


def _parent():
    return Document(
        document_id="media:ep42",
        source_type="transcript",
        language="en",
        ingested_at=1000,
        title="Episode 42",
        url="https://ex.com/ep42",
    )


def test_media_fragment_point_uri():
    assert media_fragment("file:///ep42.mp4", 742.3) == "file:///ep42.mp4#t=742.300"


def test_ocr_fills_text_via_injected_backend():
    frames = [Keyframe(timestamp_s=10.0, image_bytes=b"img")]
    ocr = lambda b: "GDP +3.4% in 2024"
    out = ocr_keyframes(frames, ocr)
    assert out[0].ocr_text == "GDP +3.4% in 2024"


def test_no_ocr_backend_passes_through():
    frames = [Keyframe(timestamp_s=10.0, image_bytes=b"img")]
    assert ocr_keyframes(frames, None)[0].ocr_text is None


def test_keyframe_documents_emitted_with_fragment_refs():
    parent = _parent()
    frames = [
        Keyframe(timestamp_s=12.5, image_bytes=b"a"),
        Keyframe(timestamp_s=30.0, image_bytes=b"b"),
    ]
    ocr = lambda b: "Unemployment 3.4%" if b == b"a" else "Q4 revenue $1.2B"
    docs = keyframe_documents(parent, "file:///ep42.mp4", "media:ep42", frames, ocr=ocr)
    assert len(docs) == 2
    d0 = docs[0]
    assert d0.source_type == "transcript"  # inherited
    assert d0.metadata["modality"] == "keyframe"
    assert d0.metadata["parent_document_id"] == "media:ep42"
    assert d0.metadata["start_s"] == 12.5
    assert d0.content == "Unemployment 3.4%"
    assert d0.content_ref == "file:///ep42.mp4#t=12.500"


def test_blank_frames_skipped():
    parent = _parent()
    frames = [Keyframe(timestamp_s=1.0, image_bytes=b"a"), Keyframe(timestamp_s=2.0, image_bytes=b"b")]
    ocr = lambda b: "   " if b == b"a" else "Real on-screen text"
    docs = keyframe_documents(parent, "m.mp4", "media:ep42", frames, ocr=ocr)
    assert len(docs) == 1  # the blank frame adds nothing
    assert docs[0].content == "Real on-screen text"


def test_no_ocr_yields_no_documents():
    parent = _parent()
    frames = [Keyframe(timestamp_s=1.0, image_bytes=b"a")]
    # No OCR backend -> no on-screen text -> no keyframe documents.
    assert keyframe_documents(parent, "m.mp4", "media:ep42", frames, ocr=None) == []


def test_max_keyframes_caps():
    parent = _parent()
    frames = [Keyframe(timestamp_s=float(i), image_bytes=b"x") for i in range(500)]
    docs = keyframe_documents(parent, "m.mp4", "media:ep42", frames, ocr=lambda b: "text here", max_keyframes=5)
    assert len(docs) == 5
