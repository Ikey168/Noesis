"""
Image asset store: content-addressed storage shared by Tracks B and C.

See ``docs/architecture/VISUAL_EVIDENCE_PLAN.md`` (Track B) and
``docs/architecture/OSINT_IMAGERY_PLAN.md`` (Track C).
"""

from __future__ import annotations

from src.ingestion.assets.imageinfo import extension_for, sniff
from src.ingestion.assets.store import ImageAsset, ImageAssetStore

__all__ = ["ImageAsset", "ImageAssetStore", "sniff", "extension_for"]
