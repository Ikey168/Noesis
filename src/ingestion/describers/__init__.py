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
from src.ingestion.describers.vision import FigureDescription, VisionDescriber

__all__ = [
    "VisionDescriber",
    "FigureDescription",
    "FigureCandidate",
    "extract_figure_captions",
    "figure_document",
    "figure_documents",
]
