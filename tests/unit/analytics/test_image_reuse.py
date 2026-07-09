"""Unit tests for image reuse detection (C2)."""

from __future__ import annotations

import io

import pytest

from src.analytics.honesty import validate_analytic_output
from src.analytics.image_reuse import find_reuse, image_provenance, image_reuse
from src.ingestion.assets.store import ImageAssetStore

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _img(shift=0, size=48):
    im = Image.new("RGB", (size, size))
    px = im.load()
    for y in range(size):
        for x in range(size):
            v = (x * 5 + shift) % 256
            px[x, y] = (v, v, v)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _rescale(data, size):
    im = Image.open(io.BytesIO(data)).resize((size, size))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def conn():
    duckdb = pytest.importorskip("duckdb")
    return duckdb.connect(":memory:")


def test_no_provenance_tables_degrades(conn):
    res = find_reuse(conn)
    assert res["findings"] == []
    assert validate_analytic_output(res) == []


def test_recycled_photo_pair_is_a_finding(conn):
    store = ImageAssetStore(conn)
    photo = _img()
    store.ingest(photo, document_id="news:floods2024", context="flood", now_ms=1)
    store.ingest(photo, document_id="blog:fire2019", context="fire", now_ms=2)
    res = find_reuse(conn)
    assert validate_analytic_output(res) == []
    assert res["finding_count"] == 1
    finding = res["findings"][0]
    assert finding["distinct_document_count"] == 2
    assert set(finding["documents"]) == {"news:floods2024", "blog:fire2019"}
    # Both appearances are cited.
    assert all(a["cited"] for a in finding["appearances"])
    assert finding["conflicting"] is True


def test_rescaled_near_duplicate_clusters(conn):
    store = ImageAssetStore(conn)
    photo = _img()
    store.ingest(photo, document_id="doc:a", now_ms=1)
    store.ingest(_rescale(photo, 120), document_id="doc:b", now_ms=2)
    res = find_reuse(conn)
    # The rescaled copy is a near-duplicate -> one cluster spanning both docs.
    assert res["finding_count"] == 1
    assert res["findings"][0]["distinct_document_count"] == 2


def test_unrelated_images_not_flagged(conn):
    store = ImageAssetStore(conn)
    store.ingest(_img(shift=0), document_id="doc:a", now_ms=1)
    store.ingest(_img(shift=140), document_id="doc:b", now_ms=2)
    assert find_reuse(conn)["finding_count"] == 0


def test_same_image_one_document_not_reuse(conn):
    store = ImageAssetStore(conn)
    photo = _img()
    store.ingest(photo, document_id="doc:a", now_ms=1)
    store.ingest(photo, document_id="doc:a", now_ms=1)  # same doc again
    assert find_reuse(conn)["finding_count"] == 0


def test_confidence_scales_with_distinct_docs(conn):
    store = ImageAssetStore(conn)
    photo = _img()
    for i, doc in enumerate(["a", "b", "c"]):
        store.ingest(photo, document_id=f"doc:{doc}", now_ms=i)
    finding = find_reuse(conn)["findings"][0]
    assert finding["distinct_document_count"] == 3
    assert finding["confidence"] == "high"


def test_topic_filter(conn):
    store = ImageAssetStore(conn)
    photo = _img()
    store.ingest(photo, document_id="doc:a", context="flooding in region", now_ms=1)
    store.ingest(photo, document_id="doc:b", context="wildfire coverage", now_ms=2)
    assert find_reuse(conn, topic="flood")["finding_count"] == 1
    assert find_reuse(conn, topic="election")["finding_count"] == 0


def test_image_provenance_query(conn):
    store = ImageAssetStore(conn)
    photo = _img()
    store.ingest(photo, document_id="doc:a", context="ctx", now_ms=1)
    prov = image_provenance(conn, store.digest(photo))
    assert prov["phash"] is not None
    assert prov["exif_note"].startswith("EXIF is claimed")
    assert [a["document_id"] for a in prov["appearances"]] == ["doc:a"]
    assert image_provenance(conn, "0" * 64)["error"]


def test_image_reuse_for_asset(conn):
    store = ImageAssetStore(conn)
    photo = _img()
    store.ingest(photo, document_id="doc:a", now_ms=1)
    store.ingest(_rescale(photo, 100), document_id="doc:b", now_ms=2)
    res = image_reuse(conn, store.digest(photo))
    assert validate_analytic_output(res) == []
    assert res["near_duplicate_count"] == 1
    assert res["near_duplicates"][0]["appearances"][0]["document_id"] == "doc:b"
