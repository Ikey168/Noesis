"""Unit tests for corrections/retractions tracking (#786)."""

from __future__ import annotations

import pytest

from src.ingestion.corrections import (
    CORRECTION_NOTICE,
    COSMETIC,
    RETRACTION,
    SILENT_SUBSTANTIVE,
    TAKEDOWN,
    UNCHANGED,
    classify_change,
    corrections_ledger,
    record_revision,
    reliability_signal,
    revision_history,
)


@pytest.fixture()
def conn():
    duckdb = pytest.importorskip("duckdb")
    return duckdb.connect(":memory:")


def test_classify_markup_and_whitespace_is_unchanged():
    assert classify_change("Hello  world.", "<p>Hello world.</p>").change_class == UNCHANGED


def test_classify_silent_substantive():
    r = classify_change("The suspect was named John Smith today.", "The suspect was named Mark Jones today.")
    assert r.change_class == SILENT_SUBSTANTIVE
    assert r.notice is False


def test_classify_correction_notice():
    r = classify_change("Prices rose 5%.", "Prices fell 5%. Correction: an earlier version misstated this.")
    assert r.change_class == CORRECTION_NOTICE
    assert r.notice is True


def test_classify_retraction():
    r = classify_change(
        "Study finds X causes Y across a large cohort over ten years of data.",
        "This paper has been retracted by the editors following review.",
    )
    assert r.change_class == RETRACTION


def test_classify_takedown():
    r = classify_change("A long article. " * 30, "removed")
    assert r.change_class == TAKEDOWN


def test_classify_cosmetic():
    # Normalized text differs only by a single trailing char -> high similarity.
    old = "The quick brown fox jumps over the lazy dog near the river bank today."
    new = old + "."
    assert classify_change(old, new).change_class in (COSMETIC, SILENT_SUBSTANTIVE)


def test_record_revision_tracks_history(conn):
    r0 = record_revision(conn, "news:1", "The mayor won 60% of the vote in the recent election.", 1)
    assert r0["revision"] == 0 and r0["change_class"] == UNCHANGED
    r1 = record_revision(conn, "news:1", "The mayor won 55% of the vote in the recent election.", 2)
    assert r1["revision"] == 1
    assert r1["change_class"] == SILENT_SUBSTANTIVE
    # An unchanged re-fetch does not add a revision.
    r2 = record_revision(conn, "news:1", "The mayor won 55% of the vote in the recent election.", 3)
    assert r2["change_class"] == UNCHANGED
    assert len(revision_history(conn, "news:1")) == 2


def test_ledger_and_reliability(conn):
    record_revision(conn, "news:1", "Original article text here about the budget vote.", 1)
    record_revision(conn, "news:1", "Rewritten article about a different budget outcome entirely.", 2)
    record_revision(conn, "blog:2", "Some claim about the economy and jobs.", 1)
    record_revision(conn, "blog:2", "Updated. Correction: an earlier version had the wrong number.", 2)
    ledger = corrections_ledger(conn)
    assert ledger["count"] == 2
    # Filter by class.
    assert corrections_ledger(conn, change_class=CORRECTION_NOTICE)["count"] == 1
    sig = reliability_signal(conn)
    assert sig["documents_with_changes"] == 2
    assert sum(sig["by_class"].values()) == 2


def test_ledger_empty_without_table(conn):
    assert corrections_ledger(conn)["entries"] == []


def test_corrections_panel_annotation_validates():
    import importlib.util
    import sys
    import types
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]

    class _StubMCP:
        def __init__(self, name):
            pass

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
            "pipeline_mcp_corr", repo_root / "tools" / "pipeline_mcp" / "server.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from src.genui.discovery import panel_def_from_annotation

        tool = {"name": "corrections_ledger", "description": "x", "meta": module.corrections_ledger._mcp_meta, "has_output_schema": True}
        panel = panel_def_from_annotation("neuronews-pipeline", tool)
        assert panel is not None and panel.type == "corrections"
    finally:
        sys.modules.pop("fastmcp", None)
