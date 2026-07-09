"""
Figure documents (Track B / B1).

Turns figures into ``document-ingest-v1`` records so they become searchable,
minable, and citable — following the media connector's precedent (one
``Document`` per segment, ``content`` carrying the text rendering, ``content_ref``
pointing at the rich artifact).

Two inputs are supported and combined:

* **Captions** parsed from the parent document's text ("Figure 3: ...") — always
  available, the caption-only fallback.
* **Image bytes** for a figure — when present, stored in the content-addressed
  :class:`ImageAssetStore` and passed to the :class:`VisionDescriber` for a
  richer description; ``content_ref`` then points at the stored asset.

The figure ``Document`` inherits the parent's ``source_type`` (so
``document-ingest-v1`` is untouched and scoping still works), marks
``metadata.modality = "image"``, and stamps the describer provenance.

See ``docs/architecture/VISUAL_EVIDENCE_PLAN.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from services.ingest.common.document_model import Document
from src.ingestion.describers.vision import FigureDescription, VisionDescriber

# "Figure 3: caption text" / "Fig. 3. caption" up to the end of the line/sentence.
_CAPTION_RE = re.compile(
    r"\b(fig(?:ure)?\.?\s*(\d+[a-z]?))\s*[:.\-—]\s*(.+?)(?:\n|$)",
    re.IGNORECASE,
)


@dataclass
class FigureCandidate:
    """A figure found in a parent document: its label, caption, and optional bytes."""

    label: str
    caption: str
    image_bytes: Optional[bytes] = None
    mime: str = "image/png"


def extract_figure_captions(text: Optional[str]) -> List[FigureCandidate]:
    """Find figure captions in document text, deduped by label (first wins)."""
    if not text:
        return []
    seen = set()
    out: List[FigureCandidate] = []
    for m in _CAPTION_RE.finditer(text):
        label = f"Figure {m.group(2)}"
        caption = m.group(3).strip()
        key = label.lower()
        if key in seen or len(caption) < 3:
            continue
        seen.add(key)
        out.append(FigureCandidate(label=label, caption=caption))
    return out


def _figure_document_id(parent_id: str, label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return f"{parent_id}#{slug}"


def figure_document(
    parent: Document,
    candidate: FigureCandidate,
    ingested_at: int,
    description: Optional[FigureDescription] = None,
    content_ref: Optional[str] = None,
) -> Document:
    """Build a figure Document from a candidate, optionally enriched.

    ``content`` is the VLM description when available, else the caption. The
    caption is always retained in metadata so it is never lost.
    """
    if description is not None:
        content = f"{candidate.label} ({description.model} description): {description.text} Caption: {candidate.caption}"
    else:
        content = f"{candidate.label}: {candidate.caption}"

    metadata = {
        "modality": "image",
        "parent_document_id": parent.document_id,
        "figure_label": candidate.label,
        "caption": candidate.caption,
    }
    if description is not None:
        metadata["describer"] = description.to_metadata()
    else:
        metadata["describer"] = None

    return Document(
        document_id=_figure_document_id(parent.document_id, candidate.label),
        source_type=parent.source_type,  # inherit; document-ingest-v1 untouched
        language=parent.language,
        ingested_at=ingested_at,
        source_id=parent.source_id,
        url=parent.url,
        title=f"{candidate.label} — {parent.title}" if parent.title else candidate.label,
        content=content,
        content_ref=content_ref,
        authors=list(parent.authors),
        created_at=parent.created_at,
        metadata=metadata,
    )


def figure_documents(
    parent: Document,
    text: Optional[str] = None,
    candidates: Optional[List[FigureCandidate]] = None,
    describer: Optional[VisionDescriber] = None,
    asset_store=None,
    ingested_at: Optional[int] = None,
    max_figures: int = 50,
) -> List[Document]:
    """Emit figure Documents for a parent document.

    Candidates come from ``candidates`` (when a connector supplies image bytes)
    or from parsing ``text`` for captions. When a candidate has image bytes and
    an ``asset_store`` is given, the bytes are stored (content-addressed) and the
    asset's ``content_ref`` is carried; when a ``describer`` is configured, the
    bytes are described, else the caption is used. ``max_figures`` caps work per
    document so a large PDF cannot stall a harvest.
    """
    ts = ingested_at if ingested_at is not None else parent.ingested_at
    cands = candidates if candidates is not None else extract_figure_captions(text)
    cands = cands[:max_figures]
    describer = describer or VisionDescriber()

    documents: List[Document] = []
    for cand in cands:
        content_ref = None
        if cand.image_bytes and asset_store is not None:
            asset = asset_store.put(cand.image_bytes, parent_document_id=parent.document_id, now_ms=ts, mime_hint=cand.mime)
            content_ref = asset.content_ref
        description = None
        if cand.image_bytes:
            description = describer.describe(cand.image_bytes, context=cand.caption, mime=cand.mime)
        documents.append(figure_document(parent, cand, ts, description=description, content_ref=content_ref))
    return documents
