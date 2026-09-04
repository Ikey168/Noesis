"""Portable namespace package export, verification, preview, and import."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
mcp=FastMCP("noesis-namespaces")


def _context() -> tuple[str,set[str]]:
    from src.config.env import resolve_env
    principal=(resolve_env("MCP_PRINCIPAL","local-reader") or "").strip(); raw=resolve_env("MCP_SCOPES","knowledge:namespace:export") or ""
    return principal,{item.strip() for item in raw.split(",") if item.strip()}


def _run(operation, *, write: bool=False):
    import duckdb

    from src.config.env import warehouse_path
    from src.kb.portable_namespaces import (
        PortableNamespaceError,
        PortableNamespaceStore,
    )
    conn=None
    try:
        conn=duckdb.connect(warehouse_path() or str(ROOT/"data/neuronews.duckdb"),read_only=not write)
        return operation(PortableNamespaceStore(conn,initialize=write))
    except PortableNamespaceError as exc: return {"ok":False,"error":exc.as_dict()}
    except Exception as exc:  # noqa: BLE001
        return {"ok":False,"error":{"code":"namespace_package_unavailable","message":str(exc)[:300]}}
    finally:
        if conn is not None: conn.close()


@mcp.tool()
def namespace_package_context() -> dict:
    """Return the principal and effective portable-namespace scopes."""
    principal,scopes=_context(); return {"principal_id":principal,"scopes":sorted(scopes)}


@mcp.tool()
def export_namespace_package(namespace: str, mode: str="full", filters: dict[str,Any] | None=None, dependency_closure: bool=True, redaction: dict[str,Any] | None=None) -> dict:
    """Export a deterministic package without mutating the source namespace."""
    _principal,scopes=_context(); return _run(lambda store:store.export(namespace,mode=mode,filters=filters,dependency_closure=dependency_closure,redaction=redaction,scopes=scopes))


@mcp.tool()
def verify_namespace_package(package: dict[str,Any]) -> dict:
    """Verify package hashes, component declarations, dependencies, and limits."""
    return _run(lambda store:store.verify(package))


@mcp.tool()
def preview_namespace_import(package: dict[str,Any], target_namespace: str, conflict_policy: str="reject", remap: dict[str,str] | None=None) -> dict:
    """Preview additions and conflicts without writing any package component."""
    _principal,scopes=_context(); return _run(lambda store:store.preview_import(package,target_namespace,conflict_policy=conflict_policy,remap=remap,scopes=scopes),write=True)


@mcp.tool()
def import_namespace_package(package: dict[str,Any], target_namespace: str, idempotency_key: str, conflict_policy: str="reject", remap: dict[str,str] | None=None, expected_preview_hash: str | None=None) -> dict:
    """Atomically import a verified package under an explicit conflict policy."""
    principal,scopes=_context(); return _run(lambda store:store.import_package(package,target_namespace,idempotency_key,conflict_policy=conflict_policy,remap=remap,scopes=scopes,principal_id=principal,expected_preview_hash=expected_preview_hash),write=True)


if __name__ == "__main__":
    from src.mcp_host.transport import run_server
    run_server(mcp)
