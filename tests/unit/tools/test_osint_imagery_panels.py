"""C2: the OSINT imagery panel annotations validate through discovery."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


class _StubMCP:
    def __init__(self, name):
        self.name = name

    def tool(self, *args, **kwargs):
        # Support both @mcp.tool and @mcp.tool(...) forms.
        if args and callable(args[0]) and not kwargs:
            args[0]._mcp_meta = None
            return args[0]

        def deco(fn):
            fn._mcp_meta = kwargs.get("meta")
            fn._mcp_output_schema = kwargs.get("output_schema")
            return fn
        return deco

    def run(self):  # pragma: no cover
        pass


@pytest.fixture()
def server(monkeypatch):
    stub = types.ModuleType("fastmcp")
    stub.FastMCP = _StubMCP
    monkeypatch.setitem(sys.modules, "fastmcp", stub)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "osint_mcp_server", REPO_ROOT / "tools" / "osint_mcp" / "server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "tool_name,panel_type",
    [
        ("image_provenance", "image_provenance"),
        ("image_reuse_findings", "image_reuse_ledger"),
    ],
)
def test_imagery_panel_annotations_validate(server, tool_name, panel_type):
    from src.genui.discovery import panel_def_from_annotation

    fn = getattr(server, tool_name)
    tool = {"name": tool_name, "description": tool_name, "meta": fn._mcp_meta, "has_output_schema": True}
    panel = panel_def_from_annotation("neuronews-osint", tool)
    assert panel is not None, f"{tool_name} annotation must be valid"
    assert panel.type == panel_type
    assert "image_assets" in panel.tables
    assert panel.ui_flag == "osint"


def test_imagery_tools_degrade_without_warehouse(server, monkeypatch):
    monkeypatch.setattr(server, "_warehouse_ro", lambda: (_ for _ in ()).throw(FileNotFoundError("no db")))
    assert "error" in server.image_provenance("abc")
    assert "error" in server.image_reuse_findings()
