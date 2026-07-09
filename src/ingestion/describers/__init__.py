"""
Ingest-time describers: turn non-text artifacts into text-native, citable
``Document`` records (Track B). See ``docs/architecture/VISUAL_EVIDENCE_PLAN.md``.
"""

from __future__ import annotations

from src.ingestion.describers.figures import (
    FigureCandidate,
    extract_figure_captions,
    figure_document,
    figure_documents,
)
from src.ingestion.describers.article_images import (
    ImageRef,
    article_figure_documents,
    extract_image_refs,
    image_upload_figure_document,
)
from src.ingestion.describers.vision import FigureDescription, VisionDescriber

__all__ = [
    "VisionDescriber",
    "FigureDescription",
    "FigureCandidate",
    "extract_figure_captions",
    "figure_document",
    "figure_documents",
    "ImageRef",
    "extract_image_refs",
    "article_figure_documents",
    "image_upload_figure_document",
]
