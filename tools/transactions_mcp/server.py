"""Authorized MCP surface for preview-bound Noesis knowledge transactions."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

mcp = FastMCP("noesis-transactions")


def _context() -> tuple[str, set[str]]:
    from src.config.env import resolve_env

    principal = (resolve_env("MCP_PRINCIPAL", "local-operator") or "").strip()
    raw_scopes = (
        resolve_env(
            "MCP_SCOPES",
            "knowledge:transaction:preview,knowledge:transaction:read",
        )
        or ""
    )
    return principal, {
        scope.strip() for scope in raw_scopes.split(",") if scope.strip()
    }


def _connection(*, read_only: bool = False):
    import duckdb

    from src.config.env import warehouse_path

    return duckdb.connect(
        warehouse_path() or str(REPO_ROOT / "data/neuronews.duckdb"),
        read_only=read_only,
    )


def _run(operation, *, write: bool = False):
    from src.kb.transactions import KnowledgeTransactionStore, TransactionError

    conn = None
    try:
        conn = _connection(read_only=not write)
        return operation(KnowledgeTransactionStore(conn, initialize=False))
    except TransactionError as exc:
        return {"ok": False, "error": exc.as_dict()}
    except Exception as exc:  # noqa: BLE001 - typed availability response
        return {
            "ok": False,
            "error": {"code": "transaction_unavailable", "message": str(exc)[:500]},
        }
    finally:
        if conn is not None:
            conn.close()


@mcp.tool()
def transaction_context() -> dict:
    """Return the configured principal and granted transaction scopes."""

    principal, scopes = _context()
    return {"principal_id": principal, "scopes": sorted(scopes)}


@mcp.tool()
def preview_mutation_batch(envelope: dict[str, Any]) -> dict:
    """Validate and preview an exact, deterministic mutation diff without writes."""

    principal, scopes = _context()
    return _run(
        lambda store: store.preview(envelope, principal_id=principal, scopes=scopes)
    )


@mcp.tool()
def commit_mutation_batch(envelope: dict[str, Any], approval_hash: str) -> dict:
    """Atomically commit the batch only when its approved preview hash is current."""

    principal, scopes = _context()
    return _run(
        lambda store: store.commit(
            envelope, approval_hash, principal_id=principal, scopes=scopes
        ),
        write=True,
    )


@mcp.tool()
def rollback_mutation_batch(batch_id: str, reason: str) -> dict:
    """Compensate a committed batch, preserving both commit and rollback audit."""

    principal, scopes = _context()
    return _run(
        lambda store: store.rollback(
            batch_id, reason, principal_id=principal, scopes=scopes
        ),
        write=True,
    )


@mcp.tool()
def replay_mutation_audit(
    batch_id: str | None = None, after_sequence: int = 0, limit: int = 100
) -> dict:
    """Replay append-only commit and rollback events in stable sequence order."""

    principal, scopes = _context()
    return _run(
        lambda store: store.audit(
            principal_id=principal,
            scopes=scopes,
            batch_id=batch_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    )


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)
