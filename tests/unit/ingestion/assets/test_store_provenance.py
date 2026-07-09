"""Unit tests for the store's Track C additions: appearances + enrichment (C1)."""

from __future__ import annotations

import io

import pytest

from src.ingestion.assets.store import ImageAssetStore

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _img_bytes(shift=0):
    img = Image.new("RGB", (48, 48))
    px = img.load()
    for y in range(48):
        for x in range(48):
            v = (x * 5 + shift) % 256
            px[x, y] = (v, v, v)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def store(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    return ImageAssetStore(duckdb.connect(":memory:"), root=str(tmp_path / "figs"))


def test_recycled_image_one_asset_many_appearances(store):
    data = _img_bytes()
    store.ingest(data, document_id="news:outletA", context="story about floods", now_ms=1000)
    store.ingest(data, document_id="blog:outletB", context="unrelated story", now_ms=2000)
    # One asset (content dedupe), two appearances.
    assert store.count() == 1
    apps = store.appearances(store.digest(data))
    assert [a["document_id"] for a in apps] == ["news:outletA", "blog:outletB"]
    assert apps[0]["context"] == "story about floods"


def test_ingest_extracts_phash_and_exif(store):
    data = _img_bytes()
    asset = store.ingest(data, document_id="doc:1")
    prov = store.get_provenance(asset.sha256)
    assert prov["phash"] is not None and len(prov["phash"]) == 16
    assert prov["exif"] == {}  # generated PNG has no EXIF, but the column is set to {}


def test_appearance_idempotent(store):
    data = _img_bytes()
    store.ingest(data, document_id="doc:1", now_ms=1)
    store.ingest(data, document_id="doc:1", now_ms=1)
    assert len(store.appearances(store.digest(data))) == 1


def test_enrich_updates_only_given_columns(store):
    data = _img_bytes()
    asset = store.put(data)
    store.enrich(asset.sha256, phash="deadbeefdeadbeef")
    assert store.get_provenance(asset.sha256)["phash"] == "deadbeefdeadbeef"
    store.enrich(asset.sha256, exif={"Make": "X"})
    prov = store.get_provenance(asset.sha256)
    assert prov["phash"] == "deadbeefdeadbeef"  # unchanged
    assert prov["exif"] == {"Make": "X"}


def test_backfill_provenance(store):
    # Two assets stored via put() (no extraction), then backfilled.
    a = store.put(_img_bytes(shift=0))
    b = store.put(_img_bytes(shift=100))
    assert store.get_provenance(a.sha256)["phash"] is None
    enriched = store.backfill_provenance()
    assert enriched == 2
    assert store.get_provenance(a.sha256)["phash"] is not None
    assert store.get_provenance(b.sha256)["phash"] is not None
    # Idempotent: nothing left to backfill.
    assert store.backfill_provenance() == 0


def test_ingest_without_document_id_still_stores(store):
    asset = store.ingest(_img_bytes())
    assert store.count() == 1
    assert store.appearances(asset.sha256) == []  # no appearance without a doc id
