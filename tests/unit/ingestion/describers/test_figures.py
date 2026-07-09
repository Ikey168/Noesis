"""Unit tests for figure caption extraction and figure Document emission (B1)."""

from __future__ import annotations

import struct
import zlib

import pytest

from services.ingest.common.document_model import Document
from src.ingestion.describers.figures import (
    FigureCandidate,
    extract_figure_captions,
    figure_documents,
)
from src.ingestion.describers.vision import VisionDescriber


def _png(w=10, h=10):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return sig + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)


def _parent():
    return Document(
        document_id="paper:123",
        source_type="paper",
        language="en",
        ingested_at=1000,
        title="A Study",
        content="abstract text",
    )


def test_extract_captions_variants():
    text = (
        "Some intro. Figure 1: Global temperature anomaly over time.\n"
        "More text. Fig. 2. Emissions by sector.\n"
        "Figure 1: a duplicate label that should be ignored.\n"
    )
    caps = extract_figure_captions(text)
    labels = [c.label for c in caps]
    assert labels == ["Figure 1", "Figure 2"]
    assert caps[0].caption.startswith("Global temperature")


def test_caption_only_figure_documents():
    parent = _parent()
    docs = figure_documents(parent, text="Figure 1: A chart of X vs Y.")
    assert len(docs) == 1
    fig = docs[0]
    assert fig.source_type == "paper"  # inherited
    assert fig.metadata["modality"] == "image"
    assert fig.metadata["parent_document_id"] == "paper:123"
    assert fig.metadata["describer"] is None  # caption-only
    assert fig.content == "Figure 1: A chart of X vs Y."
    assert fig.content_ref is None
    assert fig.document_id == "paper:123#figure-1"


def test_figure_with_bytes_stores_asset_and_sets_content_ref(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from src.ingestion.assets.store import ImageAssetStore

    store = ImageAssetStore(duckdb.connect(":memory:"), root=str(tmp_path / "figs"))
    parent = _parent()
    cand = FigureCandidate(label="Figure 3", caption="temperature chart", image_bytes=_png(), mime="image/png")
    # No key configured -> describer returns None, caption-only content, but the
    # asset is still stored and content_ref points at it.
    docs = figure_documents(parent, candidates=[cand], asset_store=store)
    fig = docs[0]
    assert fig.content_ref is not None
    assert store.count() == 1
    assert fig.metadata["describer"] is None
    assert "temperature chart" in fig.content


def test_figure_with_bytes_and_injected_describer(monkeypatch, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from src.ingestion.assets.store import ImageAssetStore

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    store = ImageAssetStore(duckdb.connect(":memory:"), root=str(tmp_path / "figs"))
    describer = VisionDescriber(complete=lambda *a: "A rising line chart of temperature anomaly.")
    parent = _parent()
    cand = FigureCandidate(label="Figure 3", caption="temp", image_bytes=_png())
    docs = figure_documents(parent, candidates=[cand], describer=describer, asset_store=store)
    fig = docs[0]
    assert "temperature anomaly" in fig.content
    assert fig.metadata["describer"]["model"]  # provenance stamped
    assert fig.content_ref is not None


def test_max_figures_caps_work():
    parent = _parent()
    text = "\n".join(f"Figure {i}: caption number {i}." for i in range(1, 11))
    docs = figure_documents(parent, text=text, max_figures=3)
    assert len(docs) == 3


def test_no_captions_yields_nothing():
    assert figure_documents(_parent(), text="No figures here at all.") == []
