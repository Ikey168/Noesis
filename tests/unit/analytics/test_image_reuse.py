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


# --- OX04 (#2044): sampled video keyframes join perceptual-hash reuse --------


def test_recycled_video_frame_is_a_finding_citing_document_and_offset(conn, tmp_path):
    from src.ingestion.connectors.media.keyframes import Keyframe, index_keyframe_assets

    store = ImageAssetStore(conn, root=str(tmp_path / "figs"))
    photo = _img(shift=3)
    store.ingest(photo, document_id="news:quake2019", context="quake photo", now_ms=1)
    # The same photo, rescaled, is a frame in a later broadcast; two other
    # frames of the same video match it too but the video is one document.
    frames = [
        Keyframe(timestamp_s=4.0, image_bytes=_img(shift=200)),
        Keyframe(timestamp_s=12.5, image_bytes=_rescale(photo, 64)),
        Keyframe(timestamp_s=13.0, image_bytes=_rescale(photo, 72)),
    ]
    indexed = index_keyframe_assets(store, "media:broadcast-2024", frames,
                                    media_ref="https://ex.com/b.mp4", now_ms=5)
    assert [i["scene_index"] for i in indexed] == [0, 1, 2]
    assert store.get(indexed[1]["sha256"]).parent_document_id == "media:broadcast-2024"
    prov = store.get_provenance(indexed[1]["sha256"])
    assert prov["phash"] and prov["exif"] == {}

    res = find_reuse(conn)
    assert validate_analytic_output(res) == []
    [finding] = [f for f in res["findings"] if "news:quake2019" in f["documents"]]
    assert finding["documents"] == ["media:broadcast-2024", "news:quake2019"]
    assert finding["distinct_document_count"] == 2  # the video is one document
    assert finding["includes_video_frames"] is True
    kinds = {a["appearance_kind"] for a in finding["appearances"]}
    assert kinds == {"still_image", "video_frame"}
    frame = next(a for a in finding["appearances"] if a["appearance_kind"] == "video_frame")
    assert frame["citation"]["document_id"] == "media:broadcast-2024"
    assert frame["citation"]["offset_s"] in (12.5, 13.0)
    assert frame["citation"]["media_fragment"].startswith("https://ex.com/b.mp4#t=")


def test_frames_from_one_video_alone_are_not_a_cross_document_finding(conn, tmp_path):
    from src.ingestion.connectors.media.keyframes import Keyframe, index_keyframe_assets

    store = ImageAssetStore(conn, root=str(tmp_path / "figs"))
    photo = _img(shift=9)
    index_keyframe_assets(store, "media:one", [Keyframe(1.0, photo), Keyframe(2.0, _rescale(photo, 60))])
    assert find_reuse(conn)["findings"] == []


def test_backfill_records_empty_exif_for_frames(conn, tmp_path):
    from src.ingestion.assets.store import frame_context

    store = ImageAssetStore(conn, root=str(tmp_path / "figs"))
    frame = _img(shift=17)
    asset = store.put(frame, parent_document_id="media:x", now_ms=1)
    store.record_appearance(asset.sha256, "media:x", context=frame_context(3.0, 0), now_ms=1)
    assert store.is_keyframe(asset.sha256) is True
    assert store.backfill_provenance() == 1
    prov = store.get_provenance(asset.sha256)
    assert prov["phash"] and prov["exif"] == {}
