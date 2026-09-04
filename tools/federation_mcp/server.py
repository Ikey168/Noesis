"""Read-only MCP access to local and configured federated knowledge sources."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

mcp = FastMCP("noesis-federation")


def _context() -> tuple[str, set[str]]:
    from src.config.env import resolve_env

    principal = (resolve_env("MCP_PRINCIPAL", "local-reader") or "").strip()
    raw = resolve_env("MCP_SCOPES", "knowledge:federation:read") or ""
    return principal, {value.strip() for value in raw.split(",") if value.strip()}


def _registry():
    import duckdb

    from src.config.env import warehouse_path
    from src.kb.federation import FederationRegistry, SQLKnowledgeAdapter

    path = warehouse_path() or str(REPO_ROOT / "data/neuronews.duckdb")

    def connect():
        return duckdb.connect(path, read_only=True)

    # The MCP boundary intentionally exposes only canonical read models.
    adapter = SQLKnowledgeAdapter(
        "noesis-local",
        connect,
        {
            "documents": ("document_id", "source_type", "language", "ingested_at"),
            "news_articles": ("id", "title", "url", "published_at"),
        },
    )
    return FederationRegistry([adapter])


@mcp.tool()
def federation_context() -> dict:
    """Return the effective principal and federation scopes."""

    principal, scopes = _context()
    return {"principal_id": principal, "scopes": sorted(scopes)}


@mcp.tool()
def federation_contract() -> dict:
    """Describe source requirements and honest merge behavior."""

    return {
        "contract": "noesis-knowledge-source-v1",
        "adapter_kinds": ["sql", "vector", "graph", "mcp", "fake"],
        "read_only": True,
        "merge": "preserve-source-scores-timestamps-and-contradictions",
    }


@mcp.tool()
def federation_sources() -> dict:
    """List authorized source identities, capabilities, limits, and freshness."""

    _principal, scopes = _context()
    try:
        return {"sources": _registry().list(scopes=scopes)}
    except Exception as exc:  # noqa: BLE001
        return {"sources": [], "error": {"code": "federation_unavailable", "message": str(exc)[:300]}}


@mcp.tool()
def federation_schema(source_id: str = "noesis-local") -> dict:
    """Discover the approved partial schema for a SQL source."""

    _principal, scopes = _context()
    try:
        adapter = _registry().adapters[source_id]
        return adapter.discover_schema(scopes=scopes)
    except Exception as exc:  # noqa: BLE001
        return {"source": source_id, "tables": {}, "error": {"code": "schema_unavailable", "message": str(exc)[:300]}}


@mcp.tool()
def query_federated_source(source_id: str, request: dict[str, Any]) -> dict:
    """Execute a typed bounded read against one approved source."""

    from src.kb.federation import FederationError

    _principal, scopes = _context()
    try:
        return _registry().adapters[source_id].query(request, scopes=scopes)
    except (KeyError, FederationError) as exc:
        error = exc.as_dict() if isinstance(exc, FederationError) else {"code": "unknown_source", "message": source_id}
        return {"contract": "noesis-source-result-v1", "items": [], "error": error}


@mcp.tool()
def federated_query(request: dict[str, Any]) -> dict:
    """Plan, execute, merge, and report partial coverage across sources."""

    from src.kb.federation import FederatedQueryEngine

    _principal, scopes = _context()
    return FederatedQueryEngine(_registry()).execute(request, scopes=scopes)


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)
