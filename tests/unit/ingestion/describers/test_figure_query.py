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


def test_figure_evidence_annotation_validates():
    # Import the pipeline server under a fastmcp stub and validate the panel.
    import importlib.util
    import sys
    import types
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[4]

    class _StubMCP:
        def __init__(self, name):
            self.name = name

        def tool(self, *args, **kwargs):
            if args and callable(args[0]) and not kwargs:
                args[0]._mcp_meta = None
                return args[0]

            def deco(fn):
                fn._mcp_meta = kwargs.get("meta")
                return fn
            return deco

        def run(self):  # pragma: no cover
            pass

    stub = types.ModuleType("fastmcp")
    stub.FastMCP = _StubMCP
    sys.modules["fastmcp"] = stub
    try:
        spec = importlib.util.spec_from_file_location(
            "pipeline_mcp_server_b3", repo_root / "tools" / "pipeline_mcp" / "server.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        from src.genui.discovery import panel_def_from_annotation

        tool = {
            "name": "figure_evidence",
            "description": "figures",
            "meta": module.figure_evidence._mcp_meta,
            "has_output_schema": True,
        }
        panel = panel_def_from_annotation("neuronews-pipeline", tool)
        assert panel is not None and panel.type == "figure_evidence"
        assert "documents" in panel.tables
        assert panel.topic_param == "topic"
    finally:
        sys.modules.pop("fastmcp", None)
