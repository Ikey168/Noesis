"""Unit tests for stdlib image sniffing."""

from __future__ import annotations

import struct
import zlib

from src.ingestion.assets.imageinfo import extension_for, sniff


def make_png(width: int, height: int) -> bytes:
    """A minimal but valid PNG (signature + IHDR) with the given dimensions."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data
    chunk += struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF)
    return sig + chunk


def make_gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 4


def make_jpeg(width: int, height: int) -> bytes:
    soi = b"\xff\xd8"
    # An APP0 segment then an SOF0 frame carrying height/width.
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00" + b"\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9
    return soi + app0 + sof0


def test_png_dimensions():
    assert sniff(make_png(640, 480)) == ("image/png", 640, 480)


def test_gif_dimensions():
    assert sniff(make_gif(120, 90)) == ("image/gif", 120, 90)


def test_jpeg_dimensions():
    assert sniff(make_jpeg(800, 600)) == ("image/jpeg", 800, 600)


def test_unknown_format_is_safe():
    assert sniff(b"not an image at all") == (None, None, None)
    assert sniff(b"\x00\x01") == (None, None, None)


def test_extension_for():
    assert extension_for("image/png") == "png"
    assert extension_for("image/jpeg") == "jpg"
    assert extension_for(None) == "bin"
    assert extension_for("application/x-weird") == "bin"
