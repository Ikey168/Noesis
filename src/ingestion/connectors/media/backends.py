"""
ffmpeg + tesseract backends for video keyframes and on-screen OCR (#823).

The keyframe core (``keyframes.py``) takes an injected frame sampler and OCR;
this module supplies the real ones, both subprocess-based because they need
system binaries the tool servers must never import:

* :class:`FfmpegSceneSampler` — scene-change keyframes via
  ``ffmpeg -vf "select='gt(scene,T)',showinfo"``, timestamps parsed from
  showinfo's ``pts_time``.
* :class:`TesseractOcr` — on-screen text via ``tesseract <frame> stdout``.

Skip-with-warning discipline throughout: an absent binary means the factory
returns ``None`` (harvest degrades to transcript-only) and a failed frame
returns ``None``/no frames — never an exception up through ``harvest()``.
Runners and binary lookups are injectable, so everything is offline-testable.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from src.ingestion.connectors.media.keyframes import Keyframe

logger = logging.getLogger(__name__)

# showinfo log lines carry the presentation timestamp of each selected frame.
_PTS_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")

DEFAULT_SCENE_THRESHOLD = 0.3
DEFAULT_MAX_FRAMES = 60

Runner = Callable[..., "subprocess.CompletedProcess"]


class FfmpegSceneSampler:
    """Scene-change keyframe sampler over the ffmpeg CLI."""

    def __init__(
        self,
        scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
        max_frames: int = DEFAULT_MAX_FRAMES,
        runner: Optional[Runner] = None,
        which: Callable[[str], Optional[str]] = shutil.which,
    ):
        self._threshold = scene_threshold
        self._max_frames = max_frames
        self._runner = runner or subprocess.run
        self._which = which

    @property
    def available(self) -> bool:
        return self._which("ffmpeg") is not None

    def __call__(self, media_bytes: bytes, file_ext: str = "mp4") -> List[Keyframe]:
        """Sample scene-change keyframes from media bytes.

        Returns ``[]`` (with a warning) when ffmpeg is absent or fails, so a
        harvest degrades to transcript-only rather than aborting.
        """
        if not self.available:
            logger.warning("keyframes: ffmpeg not found — skipping keyframe sampling")
            return []
        with tempfile.TemporaryDirectory(prefix="noesis-kf-") as tmp:
            media_path = Path(tmp) / f"input.{file_ext.lstrip('.')}"
            media_path.write_bytes(media_bytes)
            pattern = str(Path(tmp) / "frame-%04d.png")
            cmd = [
                "ffmpeg", "-hide_banner", "-nostdin",
                "-i", str(media_path),
                "-vf", f"select='gt(scene,{self._threshold})',showinfo",
                "-vsync", "vfr",
                "-frames:v", str(self._max_frames),
                pattern,
            ]
            try:
                proc = self._runner(cmd, capture_output=True, text=True, timeout=600)
            except Exception:  # noqa: BLE001 - a broken run degrades, never raises
                logger.warning("keyframes: ffmpeg run failed", exc_info=True)
                return []
            if getattr(proc, "returncode", 1) != 0:
                logger.warning("keyframes: ffmpeg exited %s", getattr(proc, "returncode", "?"))
                return []
            timestamps = [float(m) for m in _PTS_RE.findall(proc.stderr or "")]
            frames: List[Keyframe] = []
            for i, frame_path in enumerate(sorted(Path(tmp).glob("frame-*.png"))):
                ts = timestamps[i] if i < len(timestamps) else float(i)
                frames.append(Keyframe(timestamp_s=ts, image_bytes=frame_path.read_bytes()))
            return frames[: self._max_frames]


class TesseractOcr:
    """On-screen text extraction over the tesseract CLI."""

    def __init__(
        self,
        runner: Optional[Runner] = None,
        which: Callable[[str], Optional[str]] = shutil.which,
        lang: Optional[str] = None,
    ):
        self._runner = runner or subprocess.run
        self._which = which
        self._lang = lang

    @property
    def available(self) -> bool:
        return self._which("tesseract") is not None

    def __call__(self, image_bytes: bytes) -> Optional[str]:
        """OCR one frame; None when tesseract is absent or the frame fails."""
        if not self.available:
            return None
        with tempfile.TemporaryDirectory(prefix="noesis-ocr-") as tmp:
            image_path = Path(tmp) / "frame.png"
            image_path.write_bytes(image_bytes)
            cmd = ["tesseract", str(image_path), "stdout"]
            if self._lang:
                cmd += ["-l", self._lang]
            try:
                proc = self._runner(cmd, capture_output=True, text=True, timeout=120)
            except Exception:  # noqa: BLE001
                logger.debug("keyframes: tesseract run failed", exc_info=True)
                return None
            if getattr(proc, "returncode", 1) != 0:
                return None
            text = (proc.stdout or "").strip()
            return text or None


def keyframes_enabled() -> bool:
    """Env kill switch (issue #823): on by default, NOESIS_MEDIA_KEYFRAMES=off
    disables keyframe extraction in the media connector."""
    return os.getenv("NOESIS_MEDIA_KEYFRAMES", "on").strip().lower() not in ("off", "0", "false")


def default_backends() -> tuple:
    """(sampler, ocr) — each None (with a warning) when its binary is absent."""
    sampler = FfmpegSceneSampler()
    ocr = TesseractOcr()
    if not sampler.available:
        logger.warning("keyframes: ffmpeg not installed — media harvests stay transcript-only")
        sampler = None
    if not ocr.available:
        logger.warning("keyframes: tesseract not installed — keyframes would carry no text; skipping")
        ocr = None
    return (sampler, ocr)
