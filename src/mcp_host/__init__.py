"""
MCP host runtime for the API process (MCP rearchitecture plan, R1).

Supervises the repo's ``tools/*_mcp`` stdio servers from inside the
FastAPI process: pooled sessions with lazy connect and backoff restart, a
TTL discovery cache over ``tools/list``, and a non-blocking health
snapshot surfaced through ``GET /api/v1/ui/context``.
"""

from src.mcp_host.config import ServerSpec, load_server_specs
from src.mcp_host.host import (
    MCPHost,
    get_host,
    host_status,
    start_host,
    stop_host,
)

__all__ = [
    "ServerSpec",
    "load_server_specs",
    "MCPHost",
    "get_host",
    "host_status",
    "start_host",
    "stop_host",
]
