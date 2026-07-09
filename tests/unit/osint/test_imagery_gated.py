"""Unit tests for the gated imagery tier (C4): review-queue discipline."""

from __future__ import annotations

import io

import pytest

from src.osint.imagery_gated import (
    PERSON_IDENTIFICATION_SUPPORTED,
    confirm_suggestion,
    geolocate_image,
    list_review_queue,
    reverse_image_search,
)


@pytest.fixture()
def conn_with_asset(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    PIL = pytest.importorskip("PIL")
    from PIL import Image

    from src.ingestion.assets.store import ImageAssetStore

    conn = duckdb.connect(":memory:")
    store = ImageAssetStore(conn, root=str(tmp_path / "figs"))
    im = Image.new("RGB", (32, 32), (10, 20, 30))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    asset = store.ingest(buf.getvalue(), document_id="doc:a", now_ms=1)
    return conn, asset.sha256


def test_person_identification_is_permanently_off():
    assert PERSON_IDENTIFICATION_SUPPORTED is False


def test_reverse_search_no_default_provider(conn_with_asset):
    conn, sha = conn_with_asset
    res = reverse_image_search(conn, sha, provider=None)
    assert res["status"] == "no_provider_configured"


def test_reverse_search_rejects_non_corpus_hash(conn_with_asset):
    conn, _ = conn_with_asset
    res = reverse_image_search(conn, "0" * 64, provider=lambda b: [{"url": "http://x"}])
    assert res["status"] == "not_a_corpus_image"


def test_reverse_search_queues_uncited_suggestions(conn_with_asset):
    conn, sha = conn_with_asset
    provider = lambda b: [{"url": "https://other.example/story", "title": "Elsewhere"}]
    res = reverse_image_search(conn, sha, provider=provider, now_ms=10)
    assert res["status"] == "queued"
    assert res["count"] == 1
    assert res["suggestions"][0]["cited"] is False
    # It is in the queue, uncited.
    queue = list_review_queue(conn, cited=False)
    assert queue["count"] == 1


def test_queue_writes_go_to_a_separate_store(conn_with_asset, tmp_path):
    # Least privilege: the corpus asset is read from `conn`, but the review-queue
    # write lands in a *separate* queue store — the corpus connection is never
    # written to (no queue table appears there).
    duckdb = pytest.importorskip("duckdb")
    corpus, sha = conn_with_asset
    queue = duckdb.connect(str(tmp_path / "queue.duckdb"))
    provider = lambda b: [{"url": "https://other.example/story", "title": "Elsewhere"}]
    res = reverse_image_search(corpus, sha, provider=provider, now_ms=10, queue_conn=queue)
    assert res["status"] == "queued"
    # The suggestion is in the dedicated queue store...
    assert list_review_queue(queue, cited=False)["count"] == 1
    # ...and the corpus connection holds no review queue at all.
    corpus_tables = {
        r[0] for r in corpus.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert "imagery_review_queue" not in corpus_tables
    queue.close()


def test_geolocate_queue_write_goes_to_separate_store(conn_with_asset, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    corpus, sha = conn_with_asset
    queue = duckdb.connect(str(tmp_path / "queue.duckdb"))
    vlm = lambda b: [{"landmark": "a bridge", "place": "somewhere", "confidence": 0.4}]
    res = geolocate_image(corpus, sha, vlm=vlm, now_ms=5, queue_conn=queue)
    assert res["status"] == "queued"
    assert list_review_queue(queue)["count"] == 1
    corpus_tables = {
        r[0] for r in corpus.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert "imagery_review_queue" not in corpus_tables
    queue.close()


def test_geolocate_image_no_backend(conn_with_asset):
    conn, sha = conn_with_asset
    assert geolocate_image(conn, sha, vlm=None)["status"] == "no_backend_configured"


def test_geolocate_image_queues_suggestion_grade(conn_with_asset):
    conn, sha = conn_with_asset
    vlm = lambda b: [{"landmark": "a bridge", "place": "somewhere", "confidence": 0.4}]
    res = geolocate_image(conn, sha, vlm=vlm, now_ms=5)
    assert res["status"] == "queued"
    assert res["hypotheses"][0]["grade"] == "suggestion"
    assert res["hypotheses"][0]["cited"] is False


def test_confirmation_is_the_only_path_to_cited(conn_with_asset):
    conn, sha = conn_with_asset
    provider = lambda b: [{"url": "https://other.example/story"}]
    res = reverse_image_search(conn, sha, provider=provider, now_ms=10)
    sid = res["suggestions"][0]["suggestion_id"]
    # Before confirmation: uncited.
    assert list_review_queue(conn, cited=True)["count"] == 0
    # Confirmation requires an operator identity.
    assert confirm_suggestion(conn, sid, operator="")["status"] == "rejected"
    # Operator confirms -> becomes cited.
    out = confirm_suggestion(conn, sid, operator="analyst-1", now_ms=20)
    assert out["status"] == "confirmed" and out["cited"] is True
    cited = list_review_queue(conn, cited=True)
    assert cited["count"] == 1
    assert cited["items"][0]["confirmed_by"] == "analyst-1"


def test_confirm_unknown_suggestion(conn_with_asset):
    conn, _ = conn_with_asset
    # Ensure the queue table exists first.
    reverse_image_search(conn, "0" * 64, provider=lambda b: [])
    assert confirm_suggestion(conn, "sug:nope", operator="x")["status"] in ("not_found",)


def test_tools_are_registered_as_gated():
    from src.osint.investigations import is_gated

    assert is_gated("reverse_image_search")
    assert is_gated("geolocate_image")
