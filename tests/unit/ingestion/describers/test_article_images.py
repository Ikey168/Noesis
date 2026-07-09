"""Unit tests for article/upload image figure documents (B2)."""

from __future__ import annotations

import struct
import zlib

import pytest

from services.ingest.common.document_model import Document
from src.ingestion.describers.article_images import (
    article_figure_documents,
    extract_image_refs,
    image_upload_figure_document,
)


def _png(w=8, h=8):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return sig + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)


def _article(source_type="news"):
    return Document(
        document_id="news:42",
        source_type=source_type,
        language="en",
        ingested_at=5000,
        title="Big Story",
        url="https://example.com/story",
        content="body",
    )


def test_extract_og_image_and_inline():
    html = (
        '<html><head>'
        '<meta property="og:image" content="https://cdn.example.com/lead.jpg">'
        '</head><body>'
        '<img src="https://cdn.example.com/inline1.png" alt="a chart">'
        '<img src="data:image/png;base64,AAA">'  # data URI skipped
        '</body></html>'
    )
    refs = extract_image_refs(html)
    urls = [r.url for r in refs]
    assert urls == ["https://cdn.example.com/lead.jpg", "https://cdn.example.com/inline1.png"]
    assert refs[0].is_lead is True
    assert refs[1].context == "a chart"


def test_lead_photo_becomes_cited_figure_document(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from src.ingestion.assets.store import ImageAssetStore

    store = ImageAssetStore(duckdb.connect(":memory:"), root=str(tmp_path / "figs"))
    html = '<meta property="og:image" content="https://cdn.example.com/lead.jpg">'
    parent = _article()

    docs = article_figure_documents(
        parent,
        html=html,
        fetch_image=lambda url: _png(),  # injected fetch, offline
        asset_store=store,
    )
    assert len(docs) == 1
    fig = docs[0]
    assert fig.source_type == "news"  # inherits parent
    assert fig.metadata["modality"] == "image"
    assert fig.metadata["parent_document_id"] == "news:42"
    assert "lead image" in fig.metadata["figure_label"].lower()
    # The photo was stored and the figure cites it via content_ref.
    assert fig.content_ref is not None
    assert store.count() == 1


def test_no_fetch_still_emits_caption_style_figure():
    parent = _article()
    html = '<meta property="og:image" content="https://cdn.example.com/lead.jpg">'
    docs = article_figure_documents(parent, html=html)  # no fetch_image
    assert len(docs) == 1
    assert docs[0].content_ref is None
    assert docs[0].metadata["describer"] is None


def test_max_images_caps():
    parent = _article()
    imgs = "".join(f'<img src="https://x/{i}.png" alt="i{i}">' for i in range(20))
    docs = article_figure_documents(parent, html=f"<body>{imgs}</body>", max_images=3)
    assert len(docs) == 3


def test_non_image_urls_filtered():
    parent = _article()
    html = '<img src="https://x/tracker.gif?u=1" alt="ok"><img src="https://x/script.js">'
    refs = extract_image_refs(html)
    # Both are extracted as refs, but the .js is filtered at emission.
    docs = article_figure_documents(parent, html=html)
    labels = [d.metadata["figure_label"] for d in docs]
    assert len(docs) == 1  # only the .gif survives the image-extension filter


def test_image_upload_figure_document(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from src.ingestion.assets.store import ImageAssetStore

    store = ImageAssetStore(duckdb.connect(":memory:"), root=str(tmp_path / "figs"))
    parent = Document(
        document_id="upload:abc", source_type="note", language="en",
        ingested_at=1, title="my photo",
    )
    fig = image_upload_figure_document(parent, _png(), mime="image/png", asset_store=store)
    assert fig is not None
    assert fig.source_type == "note"
    assert fig.metadata["parent_document_id"] == "upload:abc"
    assert fig.content_ref is not None


def test_image_upload_empty_bytes_returns_none():
    parent = _article()
    assert image_upload_figure_document(parent, b"") is None
