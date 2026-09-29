"""Research MCP errors remain visible through FastMCP output validation."""

import asyncio

from fastmcp import Client

from tools.research_mcp import server


def test_venues_reports_warehouse_error_without_schema_failure(monkeypatch):
    def locked_warehouse():
        raise RuntimeError("warehouse locked")

    monkeypatch.setattr(server, "_warehouse_ro", locked_warehouse)

    async def call_venues():
        async with Client(server.mcp) as client:
            return await client.call_tool("venues", {})

    result = asyncio.run(call_venues())
    assert result.is_error is False
    assert result.structured_content == {"error": "warehouse locked"}
