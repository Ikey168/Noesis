"""The fastmcp-version-neutral tool introspection helper (MC01, #2746)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastmcp import FastMCP

from src.mcp_host import introspection
from src.mcp_host.introspection import (
    call_tool,
    get_tool,
    get_tool_async,
    list_tools_async,
    tool_map,
    tool_names,
    tool_schema,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER = Path(introspection.__file__).resolve()

# Built from parts so this guard does not match its own source.
_FORBIDDEN = re.compile(r"\.get_" + r"tools\(|\b_tool_" + r"manager\b")


def _server() -> FastMCP:
    mcp = FastMCP("introspection-fixture")

    @mcp.tool(annotations={"readOnlyHint": True})
    def echo(text: str) -> dict:
        """Return the text unchanged."""
        return {"text": text}

    @mcp.tool
    async def double(value: int) -> dict:
        """Double a number."""
        return {"value": value * 2}

    return mcp


def test_tool_map_lists_every_registered_tool_by_served_name():
    tools = tool_map(_server())
    assert sorted(tools) == ["double", "echo"]
    assert tools["echo"].name == "echo"


def test_tool_names_are_sorted():
    assert tool_names(_server()) == ["double", "echo"]


def test_list_tools_async_works_inside_a_running_loop():
    async def run():
        return await list_tools_async(_server())

    assert sorted(asyncio.run(run())) == ["double", "echo"]


def test_get_tool_returns_one_tool_and_raises_key_error_when_missing():
    mcp = _server()
    assert get_tool(mcp, "echo").name == "echo"
    with pytest.raises(KeyError):
        get_tool(mcp, "missing")

    async def run():
        return await get_tool_async(mcp, "double")

    assert asyncio.run(run()).name == "double"


def test_tool_schema_reads_description_schemas_and_annotations():
    schema = tool_schema(get_tool(_server(), "echo"))
    assert schema["name"] == "echo"
    assert schema["description"] == "Return the text unchanged."
    assert schema["input_schema"]["properties"] == {"text": {"type": "string"}}
    assert schema["input_schema"]["required"] == ["text"]
    assert schema["output_schema"]["type"] == "object"
    assert schema["annotations"]["readOnlyHint"] is True
    assert tool_schema(get_tool(_server(), "double"))["annotations"] is None


def test_call_tool_resolves_sync_and_async_functions():
    tools = tool_map(_server())
    assert call_tool(tools["echo"], text="hi") == {"text": "hi"}
    assert call_tool(tools["double"], value=4) == {"value": 8}


def test_removed_fastmcp_apis_are_only_used_inside_the_helper():
    """fastmcp 3 removed get_tools(); keep the API change in one module."""
    offenders = []
    for top in ("src", "tools", "tests"):
        for path in sorted((REPO_ROOT / top).rglob("*.py")):
            if path.resolve() == HELPER:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if _FORBIDDEN.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not offenders, (
        "use src.mcp_host.introspection (tool_map/get_tool) instead of "
        "the removed fastmcp tool-listing APIs:\n" + "\n".join(offenders)
    )
