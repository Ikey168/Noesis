"""Unit tests for the content-addressed image asset store."""

from __future__ import annotations

import os

import pytest

from src.ingestion.assets.store import ImageAssetStore
from tests.unit.ingestion.assets.test_imageinfo import make_gif, make_png


@pytest.fixture()
def store(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    return ImageAssetStore(duckdb.connect(":memory:"), root=str(tmp_path / "figures"))


def test_put_stores_bytes_and_indexes(store):
    data = make_png(640, 480)
    asset = store.put(data, parent_document_id="paper:123", now_ms=1000)
    assert asset.sha256 == ImageAssetStore.digest(data)
    assert asset.mime == "image/png"
    assert (asset.width, asset.height) == (640, 480)
    assert asset.parent_document_id == "paper:123"
    # content_ref is the stable relative pointer, and the bytes are on disk.
    assert asset.content_ref == asset.path
    assert os.path.exists(os.path.abspath(asset.path))
    assert store.read_bytes(asset.sha256) == data


def test_content_addressed_path_shape(store):
    asset = store.put(make_png(10, 10))
    # artifacts root / first-two-hex-chars / <sha>.png
    parts = asset.path.replace("\\", "/").split("/")
    assert parts[-2] == asset.sha256[:2]
    assert parts[-1] == f"{asset.sha256}.png"


def test_same_bytes_from_two_sources_dedupe(store):
    data = make_png(100, 100)
    first = store.put(data, parent_document_id="news:A", now_ms=1000)
    second = store.put(data, parent_document_id="blog:B", now_ms=2000)
    # One asset, stable content_ref; first-seen provenance preserved.
    assert store.count() == 1
    assert second.sha256 == first.sha256
    assert second.content_ref == first.content_ref
    assert second.parent_document_id == "news:A"
    assert second.first_seen_at == 1000


def test_distinct_images_are_distinct_assets(store):
    a = store.put(make_png(10, 10))
    b = store.put(make_gif(10, 10))
    assert a.sha256 != b.sha256
    assert store.count() == 2


def test_get_and_exists(store):
    asset = store.put(make_png(10, 10))
    assert store.exists(asset.sha256)
    assert store.get(asset.sha256).sha256 == asset.sha256
    assert store.get("0" * 64) is None
    assert not store.exists("0" * 64)


def test_list_by_parent(store):
    store.put(make_png(10, 10), parent_document_id="doc:1")
    store.put(make_gif(10, 10), parent_document_id="doc:2")
    assert len(store.list_assets()) == 2
    assert len(store.list_assets(parent_document_id="doc:1")) == 1


def test_reserved_track_c_columns_present(store):
    # C1 (#771) must be able to populate phash/exif/c2pa without a migration.
    store.put(make_png(10, 10))
    cols = [r[1] for r in store._conn.execute("PRAGMA table_info('image_assets')").fetchall()]
    assert {"phash", "exif", "c2pa"}.issubset(set(cols))


def test_persists_to_file(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    db_path = str(tmp_path / "assets.duckdb")
    root = str(tmp_path / "figures")
    store = ImageAssetStore(duckdb.connect(db_path), root=root)
    data = make_png(20, 20)
    sha = store.put(data).sha256
    store._conn.close()
    reopened = ImageAssetStore(duckdb.connect(db_path), root=root)
    assert reopened.exists(sha)
    assert reopened.read_bytes(sha) == data
