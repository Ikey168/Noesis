"""
Image provenance extraction (Track C / C1): perceptual hash + EXIF.

Corpus-internal, local, model-free. Two signals per image:

* A **perceptual hash** (dHash) robust to recompression/resizing/mild crops, for
  near-duplicate ("recycled photo") detection in C2.
* **EXIF** metadata, recorded as *claims made by the file* — never verified fact
  (EXIF is trivially editable), so callers and panels must present it as such.

Pillow is imported lazily (it is already an indirect dependency via
``pdf2image``); if it is unavailable or an image cannot be decoded, extraction
degrades to ``None`` / ``{}`` rather than raising, so ingestion never breaks on a
bad image. Heavy work stays out of the tool servers by living here, called from
the batch/backfill path.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

HASH_SIZE = 8  # dHash over a (HASH_SIZE+1 x HASH_SIZE) grid -> HASH_SIZE^2 bits


def _load_image(image_bytes: bytes):
    """Open image bytes with Pillow, or return None if unavailable/undecodable."""
    try:
        import io

        from PIL import Image  # lazy: optional heavy dependency

        return Image.open(io.BytesIO(image_bytes))
    except Exception:  # noqa: BLE001 - missing Pillow or a bad image both degrade
        logger.debug("provenance: could not open image", exc_info=True)
        return None


def perceptual_hash(image_bytes: bytes, hash_size: int = HASH_SIZE) -> Optional[str]:
    """dHash as a hex string, or None when the image can't be decoded.

    Grayscale, resized to ``(hash_size+1, hash_size)``; each bit is whether a
    pixel is brighter than its right neighbour. Robust to scale and small edits.
    """
    img = _load_image(image_bytes)
    if img is None:
        return None
    try:
        gray = img.convert("L").resize((hash_size + 1, hash_size))
        px = gray.load()
        bits = 0
        for row in range(hash_size):
            for col in range(hash_size):
                bits = (bits << 1) | (1 if px[col, row] > px[col + 1, row] else 0)
        width_hex = (hash_size * hash_size + 3) // 4
        return format(bits, f"0{width_hex}x")
    except Exception:  # noqa: BLE001
        logger.debug("provenance: perceptual_hash failed", exc_info=True)
        return None


def hamming_distance(a: Optional[str], b: Optional[str]) -> Optional[int]:
    """Bit distance between two hex hashes, or None if either is missing/misshaped."""
    if not a or not b or len(a) != len(b):
        return None
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return None


def extract_exif(image_bytes: bytes) -> Dict[str, Any]:
    """Return EXIF tags as file-*claimed* metadata (never verified), or {}.

    GPS is decoded into a nested ``GPSInfo`` mapping when present. Values are
    stringified defensively so the result is always JSON-serializable.
    """
    img = _load_image(image_bytes)
    if img is None:
        return {}
    try:
        from PIL import ExifTags  # lazy

        raw = getattr(img, "getexif", lambda: None)()
        if not raw:
            return {}
        tag_names = ExifTags.TAGS
        gps_names = ExifTags.GPSTAGS
        out: Dict[str, Any] = {}
        for tag_id, value in raw.items():
            name = tag_names.get(tag_id, str(tag_id))
            if name == "GPSInfo" and isinstance(value, dict):
                out["GPSInfo"] = {gps_names.get(k, str(k)): _coerce(v) for k, v in value.items()}
            else:
                out[name] = _coerce(value)
        return out
    except Exception:  # noqa: BLE001
        logger.debug("provenance: extract_exif failed", exc_info=True)
        return {}


def _coerce(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    return str(value)
