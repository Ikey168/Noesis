"""Annotation-parity litmus for the R2 tool annotations (ADR-001).

Loads the annotated MCP servers, lists their tools over an in-memory
FastMCP client (the true wire form, i.e. a real ``tools/list`` round
trip), and checks that:

* every static catalog panel type except the composed panels (note,
  timeline) has exactly one annotated tool counterpart, and
* every annotation that mirrors a static panel type matches the static
  ``PanelDef`` field for field, so discovery overrides can never silently
  change what a panel means.

Requires fastmcp (a server-side dev dependency, not in requirements.txt),
so the whole module skips where it is not installed.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")

from src.genui.catalog import PANEL_TYPES, get_panel_def
from src.genui.discovery import panel_def_from_annotation

REPO = Path(__file__).resolve().parents[3]

ANNOTATED_SERVERS = {
    "neuronews-arguments": REPO / "tools/argument_mcp/server.py",
    "neuronews-kg": REPO / "tools/kg_mcp/server.py",
    "neuronews-blog-feeds": REPO / "tools/blog_mcp/server.py",
    "neuronews-pipeline": REPO / "tools/pipeline_mcp/server.py",
    "neuronews-research": REPO / "tools/research_mcp/server.py",
}

COMPOSED_PANELS = {"note", "timeline"}  # ADR-001 exemptions


def _load_server(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"annotation_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.mcp


async def _wire_tools(mcp) -> list:
    from fastmcp.client import Client

    async with Client(mcp) as client:
        return await client.list_tools()


def discover_all():
    """server name -> list of cache-shaped tool dicts, via real tools/list."""
    out = {}
    for name, path in ANNOTATED_SERVERS.items():
        tools = asyncio.run(_wire_tools(_load_server(name, path)))
        out[name] = [
            {
                "name": t.name,
                "description": (t.description or "").strip(),
                "meta": t.meta if isinstance(t.meta, dict) else {},
                "has_output_schema": t.outputSchema is not None,
            }
            for t in tools
        ]
    return out


@pytest.fixture(scope="module")
def wire():
    return discover_all()


def test_every_data_panel_type_has_an_annotated_counterpart(wire):
    discovered = {}
    for server, tools in wire.items():
        for tool in tools:
            panel = panel_def_from_annotation(server, tool)
            if panel is not None:
                assert panel.type not in discovered, (
                    f"duplicate annotation for {panel.type} "
                    f"({discovered[panel.type]} and {server}:{tool['name']})"
                )
                discovered[panel.type] = f"{server}:{tool['name']}"

    expected = set(PANEL_TYPES) - COMPOSED_PANELS
    missing = expected - set(discovered)
    extra = set(discovered) - expected
    assert not missing, f"panel types without an annotated tool: {sorted(missing)}"
    assert not extra, f"annotated types not in the static catalog: {sorted(extra)}"


def test_annotations_mirror_the_static_catalog(wire):
    for server, tools in wire.items():
        for tool in tools:
            panel = panel_def_from_annotation(server, tool)
            if panel is None:
                continue
            static = get_panel_def(panel.type)
            assert static is not None
            assert panel == static, (
                f"{server}:{tool['name']} annotation drifted from the static "
                f"catalog for {panel.type}:\n  annotated: {panel}\n  static:    {static}"
            )


def test_annotated_tools_all_declare_output_schemas(wire):
    for server, tools in wire.items():
        for tool in tools:
            if "panel" in tool["meta"]:
                assert tool["has_output_schema"], (
                    f"{server}:{tool['name']} is annotated but has no outputSchema"
                )
