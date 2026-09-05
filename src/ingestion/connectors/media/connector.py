"""
Media connector: ingest audio/video files as timestamped transcript Documents.

Implements the connector interface (discover -> fetch -> parse -> Document)
for source_type="transcript". Each Whisper segment becomes its own Document
so RAG retrieval returns timestamp-linked answers ("12:34 - 15:22").

Usage::

    from src.ingestion.connectors import get_connector

    connector = get_connector("transcript")
    for doc in connector.harvest(["podcast.mp3", "https://example.com/talk.mp4"]):
        print(doc.title)            # "My Podcast [12:34 - 15:22]"
        print(doc.metadata["start_s"], doc.metadata["end_s"])
        print(doc.metadata["speaker"])   # None if no diarization
"""

from __future__ import annotations

import urllib.request
import json
from pathlib import Path
from typing import Any, Iterable, List, Optional

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
from src.ingestion.connectors.media.diarizer import assign_speakers
from src.ingestion.connectors.media.models import (
    MediaMetadata,
    TranscriptSegment,
    segment_id,
)
from src.ingestion.connectors.media.transcriber import transcribe
from src.ingestion.connectors.registry import register_connector

_AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".weba"}
_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".ts"}
def _is_url(locator: str) -> bool:
    return locator.startswith("http://") or locator.startswith("https://")


def _ext(locator: str) -> str:
    return Path(locator.split("?")[0]).suffix.lower()


def _content_type(locator: str) -> str:
    ext = _ext(locator)
    if ext in _AUDIO_EXTENSIONS:
        return f"audio/{ext.lstrip('.')}"
    if ext in _VIDEO_EXTENSIONS:
        return f"video/{ext.lstrip('.')}"
    return "application/octet-stream"


def _media_fragment_uri(base_uri: str, seg: TranscriptSegment) -> str:
    """Media Fragment URI (W3C spec): base#t=start,end"""
    return f"{base_uri}#t={seg.start_s:.3f},{seg.end_s:.3f}"


def segment_to_document(
    seg: TranscriptSegment,
    meta: MediaMetadata,
    ingested_at: int,
    index: int,
) -> Document:
    """Map a single TranscriptSegment to a Document with timestamp provenance."""
    base_uri = meta.base_uri
    fragment_uri = _media_fragment_uri(base_uri, seg) if base_uri else None
    doc_id = segment_id(meta.document_id, seg.start_s)
    title_suffix = f" [{seg.timestamp_label}]"
    title = (meta.title or "Transcript") + title_suffix

    md: dict = {
        "media_title": meta.title,
        "media_id": meta.document_id,
        "start_s": seg.start_s,
        "end_s": seg.end_s,
        "duration_s": seg.duration_s,
        "segment_index": index,
        "media_format": meta.media_format,
        "model": meta.model,
        "extractor": meta.extractor,
    }
    if seg.speaker is not None:
        md["speaker"] = seg.speaker
    if meta.duration_s:
        md["media_duration_s"] = meta.duration_s

    return Document(
        document_id=doc_id,
        source_type="transcript",
        language=meta.language,
        ingested_at=ingested_at,
        source_id=meta.source_url or (
            f"file://{Path(meta.file_path).name}" if meta.file_path else None
        ),
        url=fragment_uri,
        title=title,
        content=seg.text,
        content_ref=fragment_uri,
        metadata=md,
    )


def media_metadata_to_documents(meta: MediaMetadata, ingested_at: int) -> List[Document]:
    """
    Convert a transcribed MediaMetadata to a list of Documents, one per segment.

    Each Document carries ``start_s`` / ``end_s`` in its metadata so vector
    search results are immediately timestamp-linked. If the media has no
    segments (no transcription backend available), returns an empty list.
    """
    return [
        segment_to_document(seg, meta, ingested_at, i)
        for i, seg in enumerate(meta.segments)
        if seg.text.strip()
    ]


@register_connector
class MediaConnector(Connector):
    """
    Ingest audio/video files as timestamped transcript Documents.

    discover(query) expects a list of file paths (str/Path) or HTTP(S) URLs.
    Each Whisper segment becomes a separate Document so RAG retrieval can
    cite "at 12:34 in episode 42" rather than just "the podcast".

    Optional speaker diarization is activated by passing a HuggingFace token
    (``hf_token``) or setting the ``HF_TOKEN`` / ``HUGGINGFACE_HUB_TOKEN``
    environment variable. Requires ``pyannote.audio``.
    """

    source_type = "transcript"

    def __init__(
        self,
        model_size: str = "base",
        language: Optional[str] = None,
        hf_token: Optional[str] = None,
        diarize: bool = False,
        frame_sampler: Optional[Any] = None,
        ocr: Optional[Any] = None,
        aligner: Optional[Any] = None,
    ) -> None:
        self._model_size = model_size
        self._language = language
        self._hf_token = hf_token
        self._diarize = diarize
        # Keyframe backends (#823): injectable for tests; None means "resolve
        # the real ffmpeg/tesseract backends lazily at parse time".
        self._frame_sampler = frame_sampler
        self._ocr = ocr
        self._aligner = aligner

    def discover(self, query: Optional[Any] = None) -> Iterable[SourceRef]:
        if query is None:
            return
        items = [query] if isinstance(query, (str, Path)) else list(query)
        for item in items:
            locator = str(item)
            title = Path(locator.split("?")[0]).stem if not _is_url(locator) else locator
            yield SourceRef(
                locator=locator,
                title=title,
                metadata={"format": _ext(locator).lstrip(".")},
            )

    def fetch(self, ref: SourceRef) -> RawDocument:
        locator = ref.locator
        if _is_url(locator):
            with urllib.request.urlopen(locator, timeout=60) as resp:
                content = resp.read()
        else:
            path = Path(locator)
            if not path.exists():
                raise FileNotFoundError(f"Media file not found: {path}")
            content = path.read_bytes()
        return RawDocument(
            ref=ref,
            content=content,
            content_type=_content_type(locator),
        )

    def parse(self, raw: RawDocument) -> List[Document]:
        locator = raw.ref.locator
        title = raw.ref.title or Path(locator.split("?")[0]).stem
        file_ext = _ext(locator).lstrip(".") or "mp3"
        content = raw.content if isinstance(raw.content, bytes) else raw.content.encode()

        meta = transcribe(
            content,
            model_size=self._model_size,
            language=self._language,
            file_ext=file_ext,
        )
        meta.title = title
        meta.media_format = file_ext
        if _is_url(locator):
            meta.source_url = locator
        else:
            meta.file_path = str(Path(locator).resolve())

        if self._diarize and meta.segments:
            meta.segments = assign_speakers(content, meta.segments, self._hf_token)
            meta.speakers = sorted({s.speaker for s in meta.segments if s.speaker})

        documents = media_metadata_to_documents(meta, raw.fetched_at)
        if self._aligner is not None:
            alignments = self._aligner(content, meta.segments)
            if len(alignments) != len(documents):
                raise ValueError("alignment must preserve segment/document count")
            for document, alignment in zip(documents, alignments):
                document.metadata["word_alignment_json"] = json.dumps(alignment, ensure_ascii=False, sort_keys=True)
        documents.extend(
            self._keyframe_documents(content, locator, title, file_ext, raw.fetched_at)
        )
        return documents

    def _keyframe_documents(
        self, content: bytes, locator: str, title: str, file_ext: str, ingested_at: int
    ) -> List[Document]:
        """On-screen-text keyframe documents for a video (#823).

        Video only, on by default, disabled by NOESIS_MEDIA_KEYFRAMES=off.
        Degrades to [] when ffmpeg/tesseract are absent — a harvest never
        aborts on missing binaries.
        """
        if f".{file_ext.lstrip('.')}" not in _VIDEO_EXTENSIONS:
            return []
        from src.ingestion.connectors.media.backends import default_backends, keyframes_enabled
        from src.ingestion.connectors.media.keyframes import keyframe_documents
        from src.ingestion.connectors.media.models import media_id

        if not keyframes_enabled():
            return []
        sampler = self._frame_sampler
        ocr = self._ocr
        if sampler is None or ocr is None:
            default_sampler, default_ocr = default_backends()
            sampler = sampler or default_sampler
            ocr = ocr or default_ocr
        if sampler is None or ocr is None:
            return []  # missing binary: transcript-only, already warned

        try:
            frames = sampler(content, file_ext=file_ext)
        except TypeError:
            frames = sampler(content)  # samplers without the file_ext kwarg
        if not frames:
            return []

        is_url = _is_url(locator)
        doc_id = media_id(
            url=locator if is_url else None,
            file_path=None if is_url else str(Path(locator).resolve()),
            title=title,
        )
        parent = Document(
            document_id=doc_id,
            source_type=self.source_type,
            language=self._language or "en",
            ingested_at=ingested_at,
            title=title,
            url=locator if is_url else None,
        )
        media_ref = locator if is_url else f"file://{Path(locator).resolve()}"
        return keyframe_documents(parent, media_ref, doc_id, frames, ocr=ocr, ingested_at=ingested_at)
