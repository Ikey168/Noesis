"""
Tests for the statistics MCP server (A2): the series_explorer panel annotation
is validated through the real genui discovery validator, and the tool bodies are
exercised end-to-end against an in-memory warehouse.

fastmcp is not a test dependency, so the server module is imported under a
minimal stub that captures each tool's ``meta``/``output_schema`` and leaves the
plain function callable.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


class _StubMCP:
    def __init__(self, name):
        self.name = name

    def tool(self, **kwargs):
        def deco(fn):
            fn._mcp_meta = kwargs.get("meta")
            fn._mcp_output_schema = kwargs.get("output_schema")
            return fn
        return deco

    def run(self):  # pragma: no cover - never called in tests
        pass


@pytest.fixture()
def server(monkeypatch):
    """Import tools/statistics_mcp/server.py under a fastmcp stub."""
    stub = types.ModuleType("fastmcp")
    stub.FastMCP = _StubMCP
    monkeypatch.setitem(sys.modules, "fastmcp", stub)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    # Load by file path so the tools/ dir need not be a package.
    spec = importlib.util.spec_from_file_location(
        "statistics_mcp_server", REPO_ROOT / "tools" / "statistics_mcp" / "server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_series_explorer_annotation_validates(server):
    from src.genui.discovery import panel_def_from_annotation

    meta = server.series_explorer._mcp_meta
    assert meta is not None and "panel" in meta
    tool = {
        "name": "series_explorer",
        "description": "series explorer",
        "meta": meta,
        "has_output_schema": True,
    }
    panel = panel_def_from_annotation("neuronews-statistics", tool)
    assert panel is not None, "series_explorer panel annotation must be valid"
    assert panel.type == "series_explorer"
    assert "dataset_series" in panel.tables
    assert panel.topic_param == "topic"


def test_annotated_tool_declares_output_schema(server):
    # ADR-001 requires an outputSchema on annotated tools.
    assert server.series_explorer._mcp_output_schema is not None


@pytest.mark.parametrize(
    "tool_name,expected_type,expected_table",
    [
        ("claim_vs_data", "claim_vs_data", "claim_data_checks"),
        ("data_check_ledger", "data_check_ledger", "claim_data_checks"),
    ],
)
def test_a4_panel_annotations_validate(server, tool_name, expected_type, expected_table):
    from src.genui.discovery import panel_def_from_annotation

    fn = getattr(server, tool_name)
    tool = {
        "name": tool_name,
        "description": tool_name,
        "meta": fn._mcp_meta,
        "has_output_schema": True,
    }
    panel = panel_def_from_annotation("neuronews-statistics", tool)
    assert panel is not None, f"{tool_name} annotation must be valid"
    assert panel.type == expected_type
    assert expected_table in panel.tables


def test_a4_tools_degrade_without_warehouse(server, monkeypatch):
    monkeypatch.setattr(server, "_warehouse_ro", lambda: (_ for _ in ()).throw(FileNotFoundError("no db")))
    assert server.claim_vs_data()["checks"] == []
    assert server.data_check_ledger()["checks"] == []


def test_tools_degrade_gracefully_without_warehouse(server, monkeypatch):
    # No warehouse file -> tools return empty/valid payloads, never raise.
    monkeypatch.setattr(server, "_warehouse_ro", lambda: (_ for _ in ()).throw(FileNotFoundError("no db")))
    assert server.list_series()["series"] == []
    assert server.series_explorer()["series"] == []
    assert server.stats()["available"] is False
    assert "error" in server.get_series("wb:X:DE")


def test_tools_read_seeded_warehouse(server, monkeypatch, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from services.ingest.common.series_model import Observation, SeriesRecord
    from src.ingestion.connectors.dataset.store import ObservationStore

    db_path = str(tmp_path / "wh.duckdb")
    writer = ObservationStore(duckdb.connect(db_path))
    writer.upsert(
        SeriesRecord(
            series_id="wb:SL.UEM.TOTL.ZS:DE",
            provider="worldbank",
            title="Unemployment - Germany",
            frequency="annual",
            as_of=100,
            observations=[Observation("2023", 3.02), Observation("2024", 3.4)],
            unit="percent",
            geography="DE",
        )
    )
    writer._conn.close()

    monkeypatch.setattr(server, "_warehouse_ro", lambda: duckdb.connect(db_path, read_only=True))
    listing = server.list_series()
    assert listing["count"] == 1
    obs = server.get_observations("wb:SL.UEM.TOTL.ZS:DE")
    assert [(o["period"], o["value"]) for o in obs["observations"]] == [("2023", 3.02), ("2024", 3.4)]
    st = server.stats()
    assert st["available"] is True
    assert st["series_count"] == 1 and st["providers"] == ["worldbank"]
    panel = server.series_explorer(topic="unemployment")
    assert panel["count"] == 1 and panel["series"][0]["latest_value"] == 3.4
