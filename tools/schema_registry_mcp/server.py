"""Authorized MCP lifecycle, validation, crosswalk, and migration operations."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

mcp = FastMCP("noesis-schema-registry")


def _context() -> tuple[str, set[str]]:
    from src.config.env import resolve_env

    principal = (resolve_env("MCP_PRINCIPAL", "local-operator") or "").strip()
    raw = (
        resolve_env(
            "MCP_SCOPES",
            "knowledge:schema:read,knowledge:schema:validate",
        )
        or ""
    )
    return principal, {item.strip() for item in raw.split(",") if item.strip()}


def _connection(*, read_only: bool):
    import duckdb

    from src.config.env import warehouse_path

    return duckdb.connect(
        warehouse_path() or str(REPO_ROOT / "data/neuronews.duckdb"),
        read_only=read_only,
    )


def _run(operation, *, write: bool = False):
    from src.kb.schema_registry import SchemaRegistry, SchemaRegistryError

    conn = None
    try:
        conn = _connection(read_only=not write)
        return operation(SchemaRegistry(conn, initialize=False))
    except SchemaRegistryError as exc:
        return {"ok": False, "error": exc.as_dict()}
    except Exception as exc:  # noqa: BLE001 - stable availability response
        return {
            "ok": False,
            "error": {"code": "schema_registry_unavailable", "message": str(exc)[:500]},
        }
    finally:
        if conn is not None:
            conn.close()


@mcp.tool()
def schema_registry_context() -> dict:
    """Return the operator-controlled principal and granted registry scopes."""

    principal, scopes = _context()
    return {"principal_id": principal, "scopes": sorted(scopes)}


@mcp.tool()
def register_schema_module(definition: dict[str, Any], idempotency_key: str) -> dict:
    """Register one immutable schema, ontology, constraint, or vocabulary version."""

    principal, scopes = _context()
    return _run(
        lambda registry: registry.register(
            definition,
            idempotency_key,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
    )


@mcp.tool()
def inspect_schema_module(module_id: str) -> dict:
    """Inspect an exact immutable module identity, including provenance."""

    _principal, scopes = _context()
    return _run(lambda registry: registry.inspect(module_id, scopes=scopes))


@mcp.tool()
def resolve_schema_module(kind: str, name: str, version_spec: str) -> dict:
    """Resolve an exact semantic version or explicit compatible range."""

    _principal, scopes = _context()
    return _run(
        lambda registry: registry.resolve(kind, name, version_spec, scopes=scopes)
    )


@mcp.tool()
def validate_schema_instance(reference: dict[str, str], instance: Any) -> dict:
    """Validate an instance with machine-readable errors and module provenance."""

    _principal, scopes = _context()
    return _run(
        lambda registry: registry.validate_instance(reference, instance, scopes=scopes)
    )


@mcp.tool()
def deprecate_schema_module(module_id: str, reason: str, idempotency_key: str) -> dict:
    """Deprecate a custom module version without deleting or replacing it."""

    principal, scopes = _context()
    return _run(
        lambda registry: registry.deprecate(
            module_id,
            reason,
            idempotency_key,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
    )


@mcp.tool()
def compare_schema_modules(
    old_reference: dict[str, str], new_reference: dict[str, str]
) -> dict:
    """Classify changes as breaking, compatible, or ambiguous before use."""

    _principal, scopes = _context()
    return _run(
        lambda registry: registry.compare(old_reference, new_reference, scopes=scopes)
    )


@mcp.tool()
def register_schema_crosswalk(crosswalk: dict[str, Any], idempotency_key: str) -> dict:
    """Register a provenance-bearing field, type, or relation crosswalk."""

    principal, scopes = _context()
    return _run(
        lambda registry: registry.register_crosswalk(
            crosswalk,
            idempotency_key,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
    )


@mcp.tool()
def declare_schema_dependency(
    module_id: str,
    consumer_kind: str,
    consumer_id: str,
    detail: dict[str, Any],
) -> dict:
    """Declare a connector, extractor, index, tool, pack, or module dependency."""

    principal, scopes = _context()
    return _run(
        lambda registry: registry.declare_dependency(
            module_id,
            consumer_kind,
            consumer_id,
            detail,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
    )


@mcp.tool()
def schema_dependency_impact(module_id: str) -> dict:
    """Report affected runtime consumers and stored-object groups using lineage."""

    _principal, scopes = _context()
    return _run(lambda registry: registry.impact(module_id, scopes=scopes))


@mcp.tool()
def export_schema_registry() -> dict:
    """Export all visible module versions in deterministic content-addressed order."""

    _principal, scopes = _context()
    return _run(lambda registry: registry.export(scopes=scopes))


@mcp.tool()
def define_schema_migration(definition: dict[str, Any], idempotency_key: str) -> dict:
    """Persist an immutable reversible migration plan."""

    principal, scopes = _context()
    return _run(
        lambda registry: registry.define_migration(
            definition,
            idempotency_key,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
    )


@mcp.tool()
def preview_schema_migration(
    migration_id: str, namespace: str = "corpus", sample_size: int = 10
) -> dict:
    """Return side-effect-free counts, conflicts, samples, and a preview hash."""

    _principal, scopes = _context()
    return _run(
        lambda registry: registry.preview_migration(
            migration_id, namespace, scopes=scopes, sample_size=sample_size
        )
    )


@mcp.tool()
def execute_schema_migration(
    migration_id: str,
    preview_hash: str,
    namespace: str = "corpus",
    batch_size: int = 500,
) -> dict:
    """Execute or resume one checkpointed migration batch after exact preview."""

    principal, scopes = _context()
    return _run(
        lambda registry: registry.execute_migration(
            migration_id,
            namespace,
            preview_hash,
            principal_id=principal,
            scopes=scopes,
            batch_size=batch_size,
        ),
        write=True,
    )


@mcp.tool()
def rollback_schema_migration(
    migration_id: str, reason: str, namespace: str = "corpus"
) -> dict:
    """Compensate applied migration revisions without erasing lineage."""

    principal, scopes = _context()
    return _run(
        lambda registry: registry.rollback_migration(
            migration_id,
            namespace,
            reason,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
    )


@mcp.tool()
def replay_schema_lineage(after_sequence: int = 0, limit: int = 100) -> dict:
    """Replay append-only registry and migration lineage in sequence order."""

    _principal, scopes = _context()
    return _run(
        lambda registry: registry.lineage(
            scopes=scopes, after_sequence=after_sequence, limit=limit
        )
    )


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)
