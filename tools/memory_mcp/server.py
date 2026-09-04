"""Bounded Noesis knowledge memory and standard MCP interoperability."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
mcp=FastMCP("noesis-memory")


def _context() -> tuple[str,set[str]]:
    from src.config.env import resolve_env
    principal=(resolve_env("MCP_PRINCIPAL","local-reader") or "").strip(); raw=resolve_env("MCP_SCOPES","knowledge:memory:read") or ""
    return principal,{item.strip() for item in raw.split(",") if item.strip()}


def _run(operation,*,write: bool=False):
    import duckdb

    from src.config.env import warehouse_path
    from src.kb.memory import MemoryError, MemoryStore
    conn=None
    try:
        conn=duckdb.connect(warehouse_path() or str(ROOT/"data/neuronews.duckdb"),read_only=not write)
        return operation(MemoryStore(conn,initialize=write))
    except MemoryError as exc: return {"ok":False,"error":exc.as_dict()}
    except Exception as exc:  # noqa: BLE001
        return {"ok":False,"error":{"code":"memory_unavailable","message":str(exc)[:300]}}
    finally:
        if conn is not None: conn.close()


@mcp.tool()
def memory_capabilities() -> dict:
    """Describe typed memory kinds, lifecycle, mappings, identity, and scopes."""
    principal,scopes=_context(); return {"contract":"noesis-memory-object-v1","principal_id":principal,"scopes":sorted(scopes),"kinds":["episodic","semantic","preference","task-state"],"epistemic_statuses":["observation","inferred-summary","user-confirmed"],"standard_mcp_interop":True}


@mcp.tool()
def remember_memory(memory: dict[str,Any], idempotency_key: str) -> dict:
    """Idempotently store one provenance-bearing, explicitly scoped memory."""
    principal,scopes=_context(); return _run(lambda store:store.remember(memory,idempotency_key,principal_id=principal,scopes=scopes),write=True)


@mcp.tool()
def retrieve_memory(query: Any, scope: dict[str,Any], kinds: list[str] | None=None, limit: int=20) -> dict:
    """Rank scoped memories with relevance, recency, evidence, and score explanations."""
    principal,scopes=_context(); return _run(lambda store:store.retrieve(query,scope,principal_id=principal,scopes=scopes,kinds=kinds or [],limit=limit))


@mcp.tool()
def correct_memory(memory_id: str, replacement: dict[str,Any], idempotency_key: str, reason: str) -> dict:
    """Create a correction and supersede the prior memory without erasing it."""
    principal,scopes=_context(); return _run(lambda store:store.correct(memory_id,replacement,idempotency_key,principal_id=principal,scopes=scopes,reason=reason),write=True)


@mcp.tool()
def forget_memory(memory_id: str, reason: str) -> dict:
    """Apply the explicit forget policy while respecting legal hold."""
    principal,scopes=_context(); return _run(lambda store:store.forget(memory_id,principal_id=principal,scopes=scopes,reason=reason),write=True)


@mcp.tool()
def memory_contradictions(memory_id: str, record: bool=False) -> dict:
    """Find conflicting memories and require explicit resolution."""
    principal,scopes=_context(); return _run(lambda store:store.contradictions(memory_id,principal_id=principal,scopes=scopes,record=record),write=record)


@mcp.tool()
def consolidate_memories(memory_ids: list[str], summary: Any, idempotency_key: str) -> dict:
    """Create an inferred summary while preserving every source memory."""
    principal,scopes=_context(); return _run(lambda store:store.consolidate(memory_ids,summary,idempotency_key,principal_id=principal,scopes=scopes),write=True)


@mcp.tool()
def set_memory_policy(scope: dict[str,Any], policy: dict[str,Any]) -> dict:
    """Set retention, archive, expiration, decay, sensitivity, or legal-hold policy."""
    principal,scopes=_context(); return _run(lambda store:store.set_policy(scope,policy,principal_id=principal,scopes=scopes),write=True)


@mcp.tool()
def apply_memory_lifecycle(scope: dict[str,Any], at_ms: int | None=None) -> dict:
    """Apply archival and expiration policy without rewriting confidence history."""
    principal,scopes=_context(); return _run(lambda store:store.apply_lifecycle(scope,principal_id=principal,scopes=scopes,at_ms=at_ms),write=True)


@mcp.tool()
def export_standard_mcp_memory(scope: dict[str,Any], limit: int=100) -> dict:
    """Export supported standard MCP entities/relations with a semantic-loss report."""
    principal,scopes=_context(); return _run(lambda store:store.export_standard_mcp(scope,principal_id=principal,scopes=scopes,limit=limit))


@mcp.tool()
def import_standard_mcp_memory(payload: dict[str,Any], scope: dict[str,Any], idempotency_prefix: str, token_budget: int=10000) -> dict:
    """Import standard MCP memory under explicit scope and token bounds."""
    principal,scopes=_context(); return _run(lambda store:store.import_standard_mcp(payload,scope,idempotency_prefix,principal_id=principal,scopes=scopes,token_budget=token_budget),write=True)


if __name__ == "__main__":
    from src.mcp_host.transport import run_server
    run_server(mcp)
