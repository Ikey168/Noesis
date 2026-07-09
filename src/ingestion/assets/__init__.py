"""
Image asset store: content-addressed storage shared by Tracks B and C.

See ``docs/architecture/VISUAL_EVIDENCE_PLAN.md`` (Track B) and
``docs/architecture/OSINT_IMAGERY_PLAN.md`` (Track C).
"""

from __future__ import annotations

from src.ingestion.assets.c2pa import has_c2pa_marker, verify_c2pa
from src.ingestion.assets.imageinfo import extension_for, sniff
from src.ingestion.assets.provenance import (
    extract_exif,
    hamming_distance,
    perceptual_hash,
)
from src.ingestion.assets.store import ImageAsset, ImageAssetStore

__all__ = [
    "ImageAsset",
    "ImageAssetStore",
    "sniff",
    "extension_for",
    "perceptual_hash",
    "hamming_distance",
    "extract_exif",
    "verify_c2pa",
    "has_c2pa_marker",
]
