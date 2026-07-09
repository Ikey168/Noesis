"""
Stdlib-only image sniffing: MIME type and pixel dimensions from the header bytes.

Kept dependency-free on purpose — the asset store is imported by connectors and
(via Track C) by the tool servers, which must stay import-safe. Supports the
formats the ingestion paths actually encounter (PNG, JPEG, GIF, BMP, WebP);
unknown formats return ``(None, None, None)`` rather than raising, so an
unrecognized image is still stored, just without dimensions.
"""

from __future__ import annotations

import struct
from typing import Optional, Tuple

# (mime, width, height); any element may be None when undeterminable.
ImageInfo = Tuple[Optional[str], Optional[int], Optional[int]]

# Canonical extension per MIME type, for the content-addressed path.
_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/webp": "webp",
    "image/tiff": "tiff",
    "image/svg+xml": "svg",
}


def extension_for(mime: Optional[str]) -> str:
    """Filesystem extension for a MIME type; 'bin' when unknown."""
    return _EXT_BY_MIME.get(mime or "", "bin")


def sniff(data: bytes) -> ImageInfo:
    """Return ``(mime, width, height)`` for an image byte string."""
    if len(data) < 4:
        return (None, None, None)

    # PNG: 8-byte signature, then IHDR with width/height as big-endian u32.
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) >= 24 and data[12:16] == b"IHDR":
            width, height = struct.unpack(">II", data[16:24])
            return ("image/png", width, height)
        return ("image/png", None, None)

    # GIF: 'GIF87a'/'GIF89a', then width/height as little-endian u16.
    if data[:6] in (b"GIF87a", b"GIF89a"):
        if len(data) >= 10:
            width, height = struct.unpack("<HH", data[6:10])
            return ("image/gif", width, height)
        return ("image/gif", None, None)

    # BMP: 'BM', dimensions as little-endian i32 at offset 18/22.
    if data[:2] == b"BM":
        if len(data) >= 26:
            width, height = struct.unpack("<ii", data[18:26])
            return ("image/bmp", abs(width), abs(height))
        return ("image/bmp", None, None)

    # WebP: RIFF container with 'WEBP'.
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return ("image/webp", *_webp_dimensions(data))

    # JPEG: SOI marker, then scan for a start-of-frame marker.
    if data[:2] == b"\xff\xd8":
        return ("image/jpeg", *_jpeg_dimensions(data))

    return (None, None, None)


def _jpeg_dimensions(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # SOF0..SOF15 carry dimensions, excluding DHT(0xC4)/DAC(0xCC)/RSTn.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return (width, height)
        if i + 3 >= n:
            break
        segment_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + segment_len
    return (None, None)


def _webp_dimensions(data: bytes) -> Tuple[Optional[int], Optional[int]]:
    fmt = data[12:16]
    try:
        if fmt == b"VP8 " and len(data) >= 30:
            width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return (width, height)
        if fmt == b"VP8L" and len(data) >= 25:
            bits = struct.unpack("<I", data[21:25])[0]
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return (width, height)
        if fmt == b"VP8X" and len(data) >= 30:
            width = (data[24] | (data[25] << 8) | (data[26] << 16)) + 1
            height = (data[27] | (data[28] << 8) | (data[29] << 16)) + 1
            return (width, height)
    except struct.error:
        return (None, None)
    return (None, None)
