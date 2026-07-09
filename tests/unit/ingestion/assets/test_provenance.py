"""Unit tests for image provenance extraction (C1): pHash + EXIF."""

from __future__ import annotations

import io

import pytest

from src.ingestion.assets.provenance import extract_exif, hamming_distance, perceptual_hash

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _gradient(width=64, height=64, shift=0):
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        for x in range(width):
            v = (x * 4 + shift) % 256
            px[x, y] = (v, v, v)
    return img


def test_phash_is_stable_and_hex():
    data = _png_bytes(_gradient())
    h1 = perceptual_hash(data)
    h2 = perceptual_hash(data)
    assert h1 == h2
    assert len(h1) == 16  # 64-bit dHash -> 16 hex chars
    int(h1, 16)  # valid hex


def test_phash_survives_rescale():
    original = _gradient(64, 64)
    rescaled = original.resize((200, 200))
    h_orig = perceptual_hash(_png_bytes(original))
    h_scaled = perceptual_hash(_png_bytes(rescaled))
    # A rescaled copy of the same image is a near-duplicate (small distance).
    assert hamming_distance(h_orig, h_scaled) <= 4


def test_phash_distinguishes_different_images():
    a = perceptual_hash(_png_bytes(_gradient(64, 64, shift=0)))
    b = perceptual_hash(_png_bytes(_gradient(64, 64, shift=128)))
    assert hamming_distance(a, b) is not None
    # Shifted gradient differs meaningfully.
    assert hamming_distance(a, b) >= 1


def test_phash_none_for_undecodable():
    assert perceptual_hash(b"not an image") is None


def test_hamming_distance_guards():
    assert hamming_distance(None, "ab") is None
    assert hamming_distance("ab", "abcd") is None
    assert hamming_distance("00", "ff") == 8


def test_extract_exif_empty_when_absent():
    # A freshly-generated PNG carries no EXIF.
    assert extract_exif(_png_bytes(_gradient())) == {}


def test_extract_exif_reads_tags():
    img = _gradient(32, 32)
    exif = Image.Exif()
    exif[271] = "TestMake"  # Make
    exif[272] = "TestModel"  # Model
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    got = extract_exif(buf.getvalue())
    assert got.get("Make") == "TestMake"
    assert got.get("Model") == "TestModel"


def test_extract_exif_json_serializable():
    import json

    img = _gradient(16, 16)
    exif = Image.Exif()
    exif[305] = "Software v1"  # Software
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    got = extract_exif(buf.getvalue())
    json.dumps(got)  # must not raise
