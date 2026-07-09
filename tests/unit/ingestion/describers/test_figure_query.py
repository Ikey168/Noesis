"""Unit tests for the figure_evidence panel query (B3)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.describers.figure_query import figure_evidence


@pytest.fixture()
def conn():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect(":memory:")
    c.execute(
        """
        CREATE TABLE documents (
            document_id TEXT, source_type TEXT, title TEXT, content TEXT,
            content_ref TEXT, metadata JSON
        )
        """
    )
    return c


def _insert(conn, document_id, source_type, title, content, content_ref, metadata):
    conn.execute(
        "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
        [document_id, source_type, title, content, content_ref, json.dumps(metadata)],
    )


def test_no_documents_table_degrades():
    duckdb = pytest.importorskip("duckdb")
    empty = duckdb.connect(":memory:")
    out = figure_evidence(empty)
    assert out["figures"] == []
    assert out["count"] == 0


def test_returns_only_image_modality(conn):
    _insert(conn, "paper:1#figure-1", "paper", "Figure 1 — A", "a chart of X", "artifacts/figures/aa/x.png",
            {"modality": "image", "parent_document_id": "paper:1", "figure_label": "Figure 1"})
    _insert(conn, "paper:1", "paper", "A paper", "the abstract text", None, {})  # not a figure
    out = figure_evidence(conn)
    assert out["count"] == 1
    fig = out["figures"][0]
    assert fig["document_id"] == "paper:1#figure-1"
    assert fig["content_ref"] == "artifacts/figures/aa/x.png"
    assert fig["parent_document_id"] == "paper:1"  # cited to parent
    assert fig["figure_label"] == "Figure 1"


def test_topic_filter(conn):
    _insert(conn, "d1#f1", "news", "Fig", "temperature anomaly chart", "r1",
            {"modality": "image", "parent_document_id": "d1"})
    _insert(conn, "d2#f1", "news", "Fig", "unemployment bar chart", "r2",
            {"modality": "image", "parent_document_id": "d2"})
    assert figure_evidence(conn, topic="temperature")["count"] == 1
    assert figure_evidence(conn, topic="chart")["count"] == 2
    assert figure_evidence(conn, topic="election")["count"] == 0
