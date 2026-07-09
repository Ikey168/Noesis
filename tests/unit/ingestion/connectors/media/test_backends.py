"""Unit tests for the ffmpeg/tesseract keyframe backends (#823) — offline."""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.ingestion.connectors.media.backends import (
    FfmpegSceneSampler,
    TesseractOcr,
    keyframes_enabled,
)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_ffmpeg_runner(frame_payloads, stderr):
    """A runner that writes fake frame files into ffmpeg's output dir."""

    def runner(cmd, **kwargs):
        pattern = Path(cmd[-1])  # frame-%04d.png
        out_dir = pattern.parent
        for i, payload in enumerate(frame_payloads, start=1):
            (out_dir / f"frame-{i:04d}.png").write_bytes(payload)
        return _completed(stderr=stderr)

    return runner


def test_sampler_returns_frames_with_pts_timestamps():
    stderr = (
        "[Parsed_showinfo] n:0 pts_time:12.500 something\n"
        "[Parsed_showinfo] n:1 pts_time:47.250 something\n"
    )
    sampler = FfmpegSceneSampler(
        runner=_fake_ffmpeg_runner([b"frameA", b"frameB"], stderr),
        which=lambda name: "/usr/bin/ffmpeg",
    )
    frames = sampler(b"video-bytes", file_ext="mp4")
    assert [(f.timestamp_s, f.image_bytes) for f in frames] == [
        (12.5, b"frameA"),
        (47.25, b"frameB"),
    ]


def test_sampler_absent_binary_returns_empty():
    sampler = FfmpegSceneSampler(runner=lambda *a, **k: _completed(), which=lambda name: None)
    assert sampler.available is False
    assert sampler(b"video") == []


def test_sampler_nonzero_exit_returns_empty():
    sampler = FfmpegSceneSampler(
        runner=lambda cmd, **k: _completed(returncode=1),
        which=lambda name: "/usr/bin/ffmpeg",
    )
    assert sampler(b"video") == []


def test_sampler_caps_frames():
    stderr = "".join(f"pts_time:{i}.0\n" for i in range(10))
    sampler = FfmpegSceneSampler(
        max_frames=3,
        runner=_fake_ffmpeg_runner([b"x"] * 10, stderr),
        which=lambda name: "/usr/bin/ffmpeg",
    )
    assert len(sampler(b"video")) == 3


def test_ocr_reads_stdout():
    ocr = TesseractOcr(
        runner=lambda cmd, **k: _completed(stdout="GDP +3.4% in 2024\n"),
        which=lambda name: "/usr/bin/tesseract",
    )
    assert ocr(b"frame") == "GDP +3.4% in 2024"


def test_ocr_absent_binary_and_blank_output():
    assert TesseractOcr(runner=lambda *a, **k: _completed(), which=lambda n: None)(b"f") is None
    ocr = TesseractOcr(runner=lambda cmd, **k: _completed(stdout="   \n"), which=lambda n: "/usr/bin/tesseract")
    assert ocr(b"frame") is None


def test_keyframes_enabled_env(monkeypatch):
    monkeypatch.delenv("NOESIS_MEDIA_KEYFRAMES", raising=False)
    assert keyframes_enabled() is True
    monkeypatch.setenv("NOESIS_MEDIA_KEYFRAMES", "off")
    assert keyframes_enabled() is False


def test_connector_emits_transcript_and_keyframes(monkeypatch):
    """A video harvest yields transcript segments AND keyframe documents."""
    from src.ingestion.connectors.media import connector as media_connector
    from src.ingestion.connectors.media.keyframes import Keyframe
    from src.ingestion.connectors.media.models import MediaMetadata, TranscriptSegment

    monkeypatch.delenv("NOESIS_MEDIA_KEYFRAMES", raising=False)

    def fake_transcribe(content, **kwargs):
        return MediaMetadata(
            title="", segments=[TranscriptSegment(start_s=0, end_s=5, text="spoken words")]
        )

    monkeypatch.setattr(media_connector, "transcribe", fake_transcribe)
    sampler = lambda content, file_ext="mp4": [Keyframe(timestamp_s=30.0, image_bytes=b"img")]
    ocr = lambda image_bytes: "Unemployment 3.4%"

    conn = media_connector.MediaConnector(frame_sampler=sampler, ocr=ocr)
    raw = media_connector.RawDocument(
        ref=media_connector.SourceRef(locator="https://ex.com/ep42.mp4", title="ep42"),
        content=b"videobytes",
        content_type="video/mp4",
    )
    docs = conn.parse(raw)
    transcript = [d for d in docs if d.metadata.get("modality") != "keyframe"]
    keyframes = [d for d in docs if d.metadata.get("modality") == "keyframe"]
    assert len(transcript) == 1 and transcript[0].content == "spoken words"
    assert len(keyframes) == 1
    assert keyframes[0].content == "Unemployment 3.4%"
    assert keyframes[0].content_ref.endswith("#t=30.000")


def test_connector_audio_gets_no_keyframes(monkeypatch):
    from src.ingestion.connectors.media import connector as media_connector
    from src.ingestion.connectors.media.models import MediaMetadata, TranscriptSegment

    monkeypatch.setattr(
        media_connector, "transcribe",
        lambda content, **k: MediaMetadata(title="", segments=[TranscriptSegment(0, 3, "audio words")]),
    )
    called = []
    conn = media_connector.MediaConnector(frame_sampler=lambda *a, **k: called.append(1) or [], ocr=lambda b: "x")
    raw = media_connector.RawDocument(
        ref=media_connector.SourceRef(locator="https://ex.com/ep.mp3", title="ep"),
        content=b"audiobytes", content_type="audio/mp3",
    )
    docs = conn.parse(raw)
    assert called == []  # sampler never invoked for audio
    assert all(d.metadata.get("modality") != "keyframe" for d in docs)


def test_connector_env_off_disables_keyframes(monkeypatch):
    from src.ingestion.connectors.media import connector as media_connector
    from src.ingestion.connectors.media.models import MediaMetadata

    monkeypatch.setenv("NOESIS_MEDIA_KEYFRAMES", "off")
    monkeypatch.setattr(media_connector, "transcribe", lambda c, **k: MediaMetadata(title="", segments=[]))
    conn = media_connector.MediaConnector(
        frame_sampler=lambda *a, **k: [1 / 0], ocr=lambda b: "x"  # would raise if ever called
    )
    raw = media_connector.RawDocument(
        ref=media_connector.SourceRef(locator="https://ex.com/ep.mp4", title="ep"),
        content=b"videobytes", content_type="video/mp4",
    )
    assert conn.parse(raw) == []
