"""Version-neutral introspection of registered FastMCP tools (MC01, #2746).

fastmcp 3 removed ``FastMCP.get_tools()`` (which returned a ``{name: Tool}``
dict) in favour of the async ``FastMCP.list_tools()`` (which returns a
sequence of ``Tool`` objects), and changed ``FastMCP.get_tool(name)`` to return
``None`` instead of raising ``NotFoundError`` for an unknown name. This module
is the only place in ``src/``, ``tools/`` and ``tests/`` allowed to touch those
APIs or the private ``_tool_manager``; ``tests/unit/mcp_host/
test_introspection.py`` fails if they appear anywhere else. It feature-detects
the installed fastmcp, so it works on the pinned 2.14 line and on 3.x.

API
---
``tool_map(server) -> dict[str, Tool]``
    Every registered tool keyed by its served name. Synchronous; it runs its
    own event loop, so call it outside a running loop (tests, scripts).
``await list_tools_async(server) -> dict[str, Tool]``
    The same, for code that is already inside an event loop.
``tool_names(server) -> list[str]``
    Sorted served names.
``get_tool(server, name) -> Tool`` / ``await get_tool_async(server, name)``
    One tool by served name; raises ``KeyError`` when it is not registered.
``tool_schema(tool) -> dict``
    ``name``, ``description``, ``input_schema``, ``output_schema`` and
    ``annotations`` as plain JSON-ready values.
``call_tool(tool, **kwargs)``
    Call the tool's Python function directly (no MCP validation or
    serialisation) and await the result if it is a coroutine.
``tool_function(decorated)``
    The plain Python function behind a module-level ``@mcp.tool`` name. On
    fastmcp 2 the decorator returns a ``FunctionTool`` (function on ``.fn``);
    on fastmcp 3 it returns the function itself.

Before -> after
---------------
=====================================================  ==========================================
Before (fastmcp 2 only)                                After (fastmcp 2.14 and 3.x)
=====================================================  ==========================================
``asyncio.run(server.mcp.get_tools())``                ``tool_map(server.mcp)``
``await mcp.get_tools()``                              ``await list_tools_async(mcp)``
``sorted(asyncio.run(mcp.get_tools()))``               ``tool_names(mcp)``
``asyncio.run(mcp.get_tool("name"))``                  ``get_tool(mcp, "name")``
``mcp._tool_manager._tools`` / ``.get_tools()``        ``tool_map(mcp)``
``mcp._tool_manager.get_tool("name")``                 ``get_tool(mcp, "name")``
``tool.parameters`` / ``tool.output_schema``           unchanged, or ``tool_schema(tool)``
``v = tool.fn(**kw); asyncio.run(v) if awaitable``     ``call_tool(tool, **kw)``
``server.some_tool.fn(...)`` (decorated name)          ``tool_function(server.some_tool)(...)``
=====================================================  ==========================================

``tool.fn``, ``tool.description``, ``tool.parameters``, ``tool.output_schema``
and ``tool.annotations`` exist on ``FunctionTool`` in both lines, so code that
reads them from the returned objects needs no change. Known fastmcp 3
differences that surface through these objects: input schemas gain
``"additionalProperties": false``, and tools of mounted servers are proxy tools
without ``fn`` (no Noesis server mounts another today).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from typing import Any

__all__ = [
    "call_tool",
    "get_tool",
    "get_tool_async",
    "list_tools_async",
    "tool_function",
    "tool_map",
    "tool_names",
    "tool_schema",
]


async def list_tools_async(server: Any) -> dict[str, Any]:
    """Return ``{served name: Tool}`` for every tool registered on ``server``."""
    legacy = getattr(server, "get_tools", None)
    if legacy is not None:  # fastmcp 2.x
        return dict(await legacy())
    # fastmcp 3.x: list_tools() returns every version of every tool; skip the
    # middleware chain so the result matches what fastmcp 2's get_tools() saw.
    tools: dict[str, Any] = {}
    for tool in await server.list_tools(run_middleware=False):
        tools.setdefault(str(tool.name), tool)
    return tools


def tool_map(server: Any) -> dict[str, Any]:
    """Synchronous :func:`list_tools_async`; must not run inside an event loop."""
    return asyncio.run(list_tools_async(server))


def tool_names(server: Any) -> list[str]:
    """Sorted served names of every tool registered on ``server``."""
    return sorted(tool_map(server))


async def get_tool_async(server: Any, name: str) -> Any:
    """Return the tool served as ``name``; raise ``KeyError`` when absent."""
    try:
        tool = await server.get_tool(name)
    except Exception as exc:  # fastmcp 2.x raises NotFoundError
        if type(exc).__name__ == "NotFoundError":
            raise KeyError(name) from exc
        raise
    if tool is None:  # fastmcp 3.x returns None
        raise KeyError(name)
    return tool


def get_tool(server: Any, name: str) -> Any:
    """Synchronous :func:`get_tool_async`; must not run inside an event loop."""
    return asyncio.run(get_tool_async(server, name))


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, Mapping):
        return dict(value)
    return value


def tool_schema(tool: Any) -> dict[str, Any]:
    """Name, description, input/output schema and annotations of ``tool``."""
    return {
        "name": str(tool.name),
        "description": str(getattr(tool, "description", None) or "").strip(),
        "input_schema": dict(getattr(tool, "parameters", None) or {"type": "object"}),
        "output_schema": _plain(getattr(tool, "output_schema", None)),
        "annotations": _plain(getattr(tool, "annotations", None)),
    }


def call_tool(tool: Any, /, **kwargs: Any) -> Any:
    """Call the tool's Python function and resolve an awaitable result."""
    fn = getattr(tool, "fn", None)
    if fn is None:
        raise TypeError(
            f"tool {getattr(tool, 'name', tool)!r} has no Python function "
            "(fastmcp 3 proxy tool); call it through an MCP client instead"
        )
    value = fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def tool_function(decorated: Any) -> Any:
    """The Python function behind a module-level ``@mcp.tool``-decorated name."""
    fn = getattr(decorated, "fn", None)
    return fn if callable(fn) else decorated
