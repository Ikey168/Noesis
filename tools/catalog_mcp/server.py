"""Noesis least-privilege MCP capability catalog."""

from __future__ import annotations

import sys
from pathlib import Path

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

mcp = FastMCP("noesis-catalog")


@mcp.tool()
async def capability_catalog() -> dict:
    """Discover usable public/read Noesis capabilities.

    The result is generated from registered MCP servers and their live tool
    schemas. Operator mutations, disabled packs, empty data capabilities, and
    private domain or namespace identifiers are omitted.
    """
    from src.mcp_host.catalog import build_catalog

    conn = None
    try:
        import duckdb

        from src.config.env import warehouse_path

        path = Path(warehouse_path() or "")
        if path.is_file():
            conn = duckdb.connect(str(path), read_only=True)
    except Exception:  # noqa: BLE001 - discovery remains available without data
        conn = None
    try:
        return await build_catalog(conn=conn, include_unusable=False)
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)
