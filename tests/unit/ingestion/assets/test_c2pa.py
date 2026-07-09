"""Unit tests for C2PA content-credentials verification (C3)."""

from __future__ import annotations

import io

import pytest

from src.ingestion.assets.c2pa import (
    STATUS_INVALID,
    STATUS_NO_CREDENTIALS,
    STATUS_PRESENT_UNVERIFIED,
    STATUS_VERIFIED,
    has_c2pa_marker,
    verify_c2pa,
)


def _with_marker(base: bytes = b"\xff\xd8\xff\xe0plain") -> bytes:
    # A byte string containing a JUMBF/C2PA marker (as an embedded manifest would).
    return base + b"....jumbf....c2pa....urn:uuid:1234....contentauth...."


def test_no_marker_is_neutral():
    assert has_c2pa_marker(b"just some pixels") is False
    result = verify_c2pa(b"just some pixels")
    assert result["status"] == STATUS_NO_CREDENTIALS
    assert "neutral" in result["note"]


def test_stray_urn_is_not_a_false_positive():
    # A lone urn:uuid without a jumbf box must not read as credentials.
    assert has_c2pa_marker(b"metadata urn:uuid:abc but no manifest box") is False


def test_marker_present_but_no_backend():
    data = _with_marker()
    assert has_c2pa_marker(data) is True
    # No backend available -> present_unverified, never "invalid".
    result = verify_c2pa(data, backend=lambda b: None)
    assert result["status"] == STATUS_PRESENT_UNVERIFIED


def test_backend_verifies():
    data = _with_marker()
    backend = lambda b: {"ok": True, "manifest": {"signer": "Acme News", "edits": []}}
    result = verify_c2pa(data, backend=backend)
    assert result["status"] == STATUS_VERIFIED
    assert result["manifest"]["signer"] == "Acme News"


def test_backend_rejects_invalid():
    data = _with_marker()
    backend = lambda b: {"ok": False, "error": "signature mismatch"}
    result = verify_c2pa(data, backend=backend)
    assert result["status"] == STATUS_INVALID
    assert "signature" in result["error"]


def test_store_verify_persists(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from src.ingestion.assets.store import ImageAssetStore

    store = ImageAssetStore(duckdb.connect(":memory:"), root=str(tmp_path / "figs"))
    data = _with_marker(b"\x89PNG\r\n\x1a\n")
    asset = store.put(data)
    result = store.verify_credentials(asset.sha256)
    # No c2pa lib in the test env -> present_unverified, persisted to the column.
    assert result["status"] == STATUS_PRESENT_UNVERIFIED
    assert store.get_provenance(asset.sha256)["c2pa"]["status"] == STATUS_PRESENT_UNVERIFIED


def test_stripped_copy_is_neutral_and_reuse_link_survives(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    PIL = pytest.importorskip("PIL")
    from PIL import Image

    from src.analytics.image_reuse import image_reuse
    from src.ingestion.assets.store import ImageAssetStore

    store = ImageAssetStore(duckdb.connect(":memory:"), root=str(tmp_path / "figs"))

    # Same visual image, one "credentialed" (marker bytes appended), one stripped.
    im = Image.new("RGB", (48, 48), (120, 120, 120))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    stripped = buf.getvalue()
    credentialed = stripped + b"....jumbf....c2pa....contentauth...."

    a = store.ingest(stripped, document_id="doc:a", now_ms=1)
    b = store.ingest(credentialed, document_id="doc:b", now_ms=2)

    # The stripped copy has no credentials (neutral); the other reads present.
    assert store.verify_credentials(a.sha256)["status"] == STATUS_NO_CREDENTIALS
    assert store.verify_credentials(b.sha256)["status"] == STATUS_PRESENT_UNVERIFIED

    # They are distinct assets (different bytes) but the pHash reuse link between
    # them still resolves — C2PA verification is independent of reuse detection.
    assert a.sha256 != b.sha256
    near = image_reuse(store._conn, a.sha256)
    assert near["near_duplicate_count"] == 1
    assert near["near_duplicates"][0]["sha256"] == b.sha256
