"""
Article and upload images as figure documents (Track B / B2).

Extends the B1 figure path from paper figures to the images news, blog, and web
documents carry — the lead photo (`og:image`) and inline `<img>` images — and to
standalone image uploads. Each becomes a searchable, citable figure `Document`
through the same describer + asset-store path, so the pipeline stays text-native.

The HTTP image fetcher is injectable so the whole path runs offline in tests.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, List, Optional

from services.ingest.common.document_model import Document
from src.ingestion.describers.figures import FigureCandidate, figure_documents
from src.ingestion.describers.vision import VisionDescriber

logger = logging.getLogger(__name__)

_OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image|twitter:image)["'][^>]*>""",
    re.IGNORECASE,
)
_CONTENT_RE = re.compile(r"""content\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_ALT_RE = re.compile(r"""\balt\s*=\s*["']([^"']*)["']""", re.IGNORECASE)

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


@dataclass
class ImageRef:
    """An image referenced by a document: its URL, alt/caption context, and role."""

    url: str
    context: str = ""
    is_lead: bool = False


def extract_image_refs(html: Optional[str]) -> List[ImageRef]:
    """Extract the lead image (og:image/twitter:image) and inline <img> images
    from article HTML, deduped by URL with the lead first."""
    if not html:
        return []
    refs: List[ImageRef] = []
    seen = set()

    for tag in _OG_IMAGE_RE.findall(html):
        m = _CONTENT_RE.search(tag)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            refs.append(ImageRef(url=m.group(1), context="lead image", is_lead=True))

    for tag in _IMG_RE.findall(html):
        src = _SRC_RE.search(tag)
        if not src:
            continue
        url = src.group(1)
        if url in seen or url.startswith("data:"):
            continue
        seen.add(url)
        alt = _ALT_RE.search(tag)
        refs.append(ImageRef(url=url, context=(alt.group(1).strip() if alt else "")))

    return refs


def _looks_like_image(url: str) -> bool:
    return url.lower().split("?")[0].endswith(_IMAGE_EXT)


def article_figure_documents(
    parent: Document,
    html: Optional[str] = None,
    image_refs: Optional[List[ImageRef]] = None,
    fetch_image: Optional[Callable[[str], Optional[bytes]]] = None,
    describer: Optional[VisionDescriber] = None,
    asset_store=None,
    max_images: int = 8,
    ingested_at: Optional[int] = None,
) -> List[Document]:
    """Emit figure Documents for an article's images.

    Images are fetched via ``fetch_image`` (injectable; skipped when None so a
    caption-style figure with no bytes is still emitted from context). The lead
    image is labeled "Figure 1"; inline images follow. Reuses the B1 emission so
    the asset store + describer behaviour is identical to paper figures.
    """
    refs = image_refs if image_refs is not None else extract_image_refs(html)
    refs = [r for r in refs if r.url and (_looks_like_image(r.url) or r.is_lead)][:max_images]
    if not refs:
        return []

    candidates: List[FigureCandidate] = []
    for i, ref in enumerate(refs):
        label = "Figure 1 (lead image)" if ref.is_lead else f"Figure {i + 1}"
        image_bytes = None
        if fetch_image is not None:
            try:
                image_bytes = fetch_image(ref.url)
            except Exception:  # noqa: BLE001 - a broken image URL is skipped, not fatal
                logger.debug("article_images: fetch failed for %s", ref.url, exc_info=True)
                image_bytes = None
        caption = ref.context or f"image from {parent.title or parent.document_id}"
        candidates.append(FigureCandidate(label=label, caption=caption, image_bytes=image_bytes))

    return figure_documents(
        parent,
        candidates=candidates,
        describer=describer,
        asset_store=asset_store,
        ingested_at=ingested_at,
        max_figures=max_images,
    )


def image_upload_figure_document(
    parent: Document,
    image_bytes: bytes,
    mime: str = "image/png",
    caption: str = "",
    describer: Optional[VisionDescriber] = None,
    asset_store=None,
    ingested_at: Optional[int] = None,
) -> Optional[Document]:
    """A standalone uploaded image becomes one figure Document whose parent is
    the upload itself."""
    if not image_bytes:
        return None
    candidate = FigureCandidate(
        label="Figure 1",
        caption=caption or f"uploaded image {parent.title or parent.document_id}",
        image_bytes=image_bytes,
        mime=mime,
    )
    docs = figure_documents(
        parent,
        candidates=[candidate],
        describer=describer,
        asset_store=asset_store,
        ingested_at=ingested_at,
    )
    return docs[0] if docs else None
