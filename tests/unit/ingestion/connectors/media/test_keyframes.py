"""Unit tests for video keyframes + on-screen OCR (#782)."""

from __future__ import annotations

import pytest

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
    def ocr(b):
        return "GDP +3.4% in 2024"
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
    def ocr(b):
        return "Unemployment 3.4%" if b == b"a" else "Q4 revenue $1.2B"
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
    def ocr(b):
        return "   " if b == b"a" else "Real on-screen text"
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


# --- OX04 (#2044): keyframes indexed as corpus image assets ------------------


def _video_raw(media_connector):
    return media_connector.RawDocument(
        ref=media_connector.SourceRef(locator="https://ex.com/ep7.mp4", title="ep7"),
        content=b"videobytes",
        content_type="video/mp4",
    )


def test_connector_indexes_sampled_frames_as_assets(monkeypatch, tmp_path):
    import json

    duckdb = pytest.importorskip("duckdb")
    from src.ingestion.assets.store import ImageAssetStore
    from src.ingestion.connectors.media import connector as media_connector
    from src.ingestion.connectors.media.models import MediaMetadata

    monkeypatch.delenv("NOESIS_MEDIA_KEYFRAMES", raising=False)
    monkeypatch.setattr(media_connector, "transcribe", lambda c, **k: MediaMetadata(title="", segments=[]))
    store = ImageAssetStore(duckdb.connect(), root=str(tmp_path / "figs"))

    def sampler(content, file_ext="mp4"):
        return [Keyframe(timestamp_s=2.0, image_bytes=b"frame-a"), Keyframe(timestamp_s=9.5, image_bytes=b"frame-b"),
                Keyframe(timestamp_s=11.0, image_bytes=None)]

    conn = media_connector.MediaConnector(frame_sampler=sampler, ocr=lambda b: None, asset_store=store)
    conn.parse(_video_raw(media_connector))
    assets = store.list_assets()
    assert len(assets) == 2  # a frame without bytes is skipped
    parents = {a["parent_document_id"] for a in assets}
    assert len(parents) == 1
    contexts = [json.loads(store.appearances(a["sha256"])[0]["context"]) for a in assets]
    assert sorted(c["offset_s"] for c in contexts) == [2.0, 9.5]
    assert sorted(c["scene_index"] for c in contexts) == [0, 1]
    assert all(c["kind"] == "video_frame" for c in contexts)
    for a in assets:
        assert store.get_provenance(a["sha256"])["exif"] == {}


def test_connector_without_ffmpeg_writes_no_asset_rows(monkeypatch, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from src.ingestion.assets.store import ImageAssetStore
    from src.ingestion.connectors.media import backends
    from src.ingestion.connectors.media import connector as media_connector
    from src.ingestion.connectors.media.models import MediaMetadata

    monkeypatch.delenv("NOESIS_MEDIA_KEYFRAMES", raising=False)
    monkeypatch.setattr(media_connector, "transcribe", lambda c, **k: MediaMetadata(title="", segments=[]))
    monkeypatch.setattr(backends, "default_backends", lambda: (None, None))
    store = ImageAssetStore(duckdb.connect(), root=str(tmp_path / "figs"))
    conn = media_connector.MediaConnector(asset_store=store)
    assert conn.parse(_video_raw(media_connector)) == []
    assert store.count() == 0


def test_frame_indexing_respects_the_max_frames_budget(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from src.ingestion.assets.store import ImageAssetStore
    from src.ingestion.connectors.media.keyframes import index_keyframe_assets

    store = ImageAssetStore(duckdb.connect(), root=str(tmp_path / "figs"))
    frames = [Keyframe(timestamp_s=float(i), image_bytes=f"f{i}".encode()) for i in range(10)]
    assert len(index_keyframe_assets(store, "media:m", frames, max_keyframes=3)) == 3
    assert store.count() == 3
