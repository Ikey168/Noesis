"""
Video keyframes + on-screen text OCR (candidate track #782).

Talks and news broadcasts put load-bearing numbers on screen (chyrons, slides,
lower thirds), not in speech — the transcript misses them entirely. This module
turns keyframes into timestamp-linked ``Document`` segments alongside the
transcript, so a semantic query returns the slide the words never say.

Following the media connector's precedent, each keyframe becomes one
``Document`` whose ``content`` is the OCR'd on-screen text and whose
``content_ref`` is a Media Fragment URI (``base#t=<seconds>``) a player can seek.

Frame extraction (scene-change sampling) and OCR are **injected** — they need
heavy binaries (ffmpeg, tesseract) that stay out of the tool servers — so this
module is testable offline and degrades to nothing when they are unavailable.

See ``docs/architecture/BEYOND_TEXT_ROADMAP.md`` §4.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from services.ingest.common.document_model import Document

logger = logging.getLogger(__name__)

# Cap keyframes per video so a long recording cannot stall a harvest.
DEFAULT_MAX_KEYFRAMES = 200

# Injected callables:
#   FrameSampler(video_ref) -> List[(timestamp_s, image_bytes)]  (scene changes)
#   Ocr(image_bytes) -> str                                      (on-screen text)
FrameSampler = Callable[[str], List["Keyframe"]]
Ocr = Callable[[bytes], Optional[str]]


@dataclass
class Keyframe:
    timestamp_s: float
    image_bytes: Optional[bytes] = None
    ocr_text: Optional[str] = None


def media_fragment(base_uri: str, timestamp_s: float) -> str:
    """A point-in-time Media Fragment URI (W3C): ``base#t=<seconds>``."""
    return f"{base_uri}#t={timestamp_s:.3f}"


def _keyframe_id(media_id: str, timestamp_s: float) -> str:
    return f"{media_id}#kf={timestamp_s:.3f}"


def keyframe_document(
    parent: Document,
    media_ref: str,
    media_id: str,
    keyframe: Keyframe,
    ingested_at: int,
) -> Document:
    """Build a Document for one keyframe's on-screen text."""
    text = (keyframe.ocr_text or "").strip()
    return Document(
        document_id=_keyframe_id(media_id, keyframe.timestamp_s),
        source_type=parent.source_type,  # inherit (transcript); enum untouched
        language=parent.language,
        ingested_at=ingested_at,
        source_id=parent.source_id,
        url=parent.url,
        title=parent.title,
        content=text or None,
        content_ref=media_fragment(media_ref, keyframe.timestamp_s),
        authors=list(parent.authors),
        created_at=parent.created_at,
        metadata={
            "modality": "keyframe",
            "parent_document_id": parent.document_id,
            "start_s": keyframe.timestamp_s,
            "on_screen_text": True,
        },
    )


def ocr_keyframes(keyframes: List[Keyframe], ocr: Optional[Ocr]) -> List[Keyframe]:
    """Fill ``ocr_text`` for keyframes that have image bytes, via the injected
    OCR. Without an OCR backend, keyframes pass through unchanged (no text)."""
    if ocr is None:
        return keyframes
    out: List[Keyframe] = []
    for kf in keyframes:
        text = kf.ocr_text
        if text is None and kf.image_bytes:
            try:
                text = ocr(kf.image_bytes)
            except Exception:  # noqa: BLE001 - a bad frame is skipped, not fatal
                logger.debug("keyframe OCR failed at %.3fs", kf.timestamp_s, exc_info=True)
                text = None
        out.append(Keyframe(timestamp_s=kf.timestamp_s, image_bytes=kf.image_bytes, ocr_text=text))
    return out


def keyframe_documents(
    parent: Document,
    media_ref: str,
    media_id: str,
    keyframes: List[Keyframe],
    ocr: Optional[Ocr] = None,
    ingested_at: Optional[int] = None,
    max_keyframes: int = DEFAULT_MAX_KEYFRAMES,
    min_chars: int = 2,
) -> List[Document]:
    """Emit a Document per keyframe that carries legible on-screen text.

    Keyframes are OCR'd (via the injected backend), capped, and only those with
    at least ``min_chars`` of text become documents — a blank frame adds nothing.
    """
    ts = ingested_at if ingested_at is not None else parent.ingested_at
    frames = ocr_keyframes(keyframes[:max_keyframes], ocr)
    documents: List[Document] = []
    for kf in frames:
        if not kf.ocr_text or len(kf.ocr_text.strip()) < min_chars:
            continue
        documents.append(keyframe_document(parent, media_ref, media_id, kf, ts))
    return documents
