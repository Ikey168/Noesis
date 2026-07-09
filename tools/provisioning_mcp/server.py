"""
NeuroNews Provisioning plane - MCP server (R8 / Track P).

The read/write surface over the provisioning plane: an agent deploys a
namespaced knowledge graph, binds the sources that feed it (explicitly or by a
quality criterion), routes matching documents into the namespace, and the
canvas grows a scoped panel for it via R2 discovery. Teardown archives, never
silently deletes. Every step is registered in an append-only lineage log.

Tool surface:
  kg_deploy(name, description?, ontology?, approve=False)   deploy (approval-gated)
  kg_attach_sources(kg, sources?, criteria?)                bind feeds/connectors
  kg_ingest(kg, backfill_days?)                             route matching docs
  kg_status(kg)                                             counts / health / lag
  kg_list(include_archived=False)                           deployed KGs
  kg_lineage(kg?, limit=50)                                 provisioning event log
  kg_teardown(kg, confirm=False)                            archive + detach
  kg_view(kg?)                                              annotated `provisioned_kg` panel

Write authority: the write tools open the DuckDB warehouse read-write and hold
a process-level lock for the operation, mirroring the pipeline server's
trigger tools; in the deployed system the write path is the API process that
owns the single warehouse writer. Read tools open the warehouse read-only.

Design constraints (as for every tool server): stdlib + fastmcp at import
time, lazy imports inside tools, guardrails enforced in
:mod:`src.provisioning`.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, List, Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

mcp = FastMCP("neuronews-provisioning")

# Serialises every provisioning write in this process (the warehouse is a
# single-writer store; provisioning never runs two writes at once).
_WRITE_LOCK = threading.Lock()


def _db_path() -> str:
    from src.config.env import warehouse_path
    return warehouse_path(str(REPO_ROOT / "data" / "neuronews.duckdb"))


def _warehouse_ro():
    import duckdb

    path = _db_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"warehouse not found at {path}")
    return duckdb.connect(path, read_only=True)


def _warehouse_rw():
    import duckdb

    path = _db_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"warehouse not found at {path} - start the API once to seed it, "
            f"or set NEURONEWS_DB_PATH"
        )
    return duckdb.connect(path, read_only=False)


# --------------------------------------------------------------------------- #
# Write tools (guardrailed; hold the write lock for the operation)
# --------------------------------------------------------------------------- #

@mcp.tool
def kg_deploy(
    name: str,
    description: str = "",
    ontology: Optional[dict] = None,
    approve: bool = False,
    backend: str = "table-prefix",
) -> dict:
    """Deploy a namespaced knowledge graph (approval-gated).

    Without ``approve`` this returns a free dry-run preview and writes nothing;
    re-run with ``approve=true`` to execute. Re-deploying an existing name
    converges (idempotent upsert) rather than duplicating.

    Args:
        name: KG identifier, lowercase ``[a-z][a-z0-9_]*``.
        description: human-readable summary.
        ontology: optional ontology hint (stored as JSON).
        approve: must be true to actually deploy.
        backend: ``table-prefix`` (default; tables in the shared warehouse) or
            ``attached`` (the KG gets its own DuckDB database file).
    """
    from src.provisioning import Provisioner

    if not approve:
        try:
            con = _warehouse_ro()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            return Provisioner(con, ensure=False).deploy(
                name, description, ontology, approve=False, backend=backend
            )
        finally:
            con.close()
    try:
        con = _warehouse_rw()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        prov = Provisioner(con, lock=_WRITE_LOCK)
        return prov.deploy(name, description, ontology, approve=True, backend=backend)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def kg_attach_pipeline(
    kg: str,
    connector: str,
    connector_type: str,
    config: Optional[dict] = None,
    contract: Optional[str] = None,
    approve: bool = False,
) -> dict:
    """Bind a pipeline (a connector or feed) to a deployed KG, contract-validated
    at attach. Approval-gated (the connector will run on ingest); idempotent by
    ``(kg, connector)``.

    Args:
        kg: the deployed KG name.
        connector: a name for this binding (e.g. "energy-rss").
        connector_type: the connector kind (e.g. "rss", "document").
        config: connector config (e.g. {"url": "http://.../feed.xml"}).
        contract: optional ingest contract id override.
        approve: must be true to bind (a preview is returned otherwise).
    """
    from src.provisioning import Provisioner

    if not approve:
        try:
            con = _warehouse_ro()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            return Provisioner(con, ensure=False).attach_pipeline(
                kg, connector, connector_type, config=config, contract=contract, approve=False
            )
        finally:
            con.close()
    try:
        con = _warehouse_rw()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        prov = Provisioner(con, lock=_WRITE_LOCK)
        return prov.attach_pipeline(
            kg, connector, connector_type, config=config, contract=contract, approve=True
        )
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def kg_attach_sources(
    kg: str,
    sources: Optional[List[str]] = None,
    criteria: Optional[dict] = None,
) -> dict:
    """Bind sources to a deployed KG, explicitly or by a quality criterion.

    ``criteria`` is resolved against the outlet transparency scores; supported
    keys: ``min_transparency`` (composite score), ``min_attribution``,
    ``type`` (source_type). Idempotent by ``(kg, source)``.

    Args:
        kg: the deployed KG name.
        sources: explicit source names to bind.
        criteria: quality criterion, e.g. {"min_transparency": 0.7, "type": "news"}.
    """
    from src.provisioning import Provisioner

    try:
        con = _warehouse_rw()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        prov = Provisioner(con, lock=_WRITE_LOCK)
        return prov.attach_sources(kg, sources=sources, criteria=criteria)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def kg_ingest(kg: str, backfill_days: Optional[int] = None) -> dict:
    """Route the bound sources' documents (and claims / derived entities) into
    the KG namespace. Rate-capped and idempotent (re-ingest converges).

    Args:
        kg: the deployed KG name.
        backfill_days: optional lookback window for the routed corpus.
    """
    from src.provisioning import Provisioner
    from src.provisioning.pipeline_runner import build_pipeline_runner

    try:
        con = _warehouse_rw()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        # M3.1: run bound connectors for real (the live pipeline runner harvests
        # documents into the corpus) before routing them into the namespace.
        prov = Provisioner(
            con, lock=_WRITE_LOCK, pipeline_runner=build_pipeline_runner(con)
        )
        return prov.ingest(kg, backfill_days=backfill_days)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def kg_teardown(kg: str, confirm: bool = False) -> dict:
    """Archive a KG: rename its namespace tables aside (never delete), detach
    its sources, mark it archived. Confirm-gated; never touches shared tables.

    Args:
        kg: the KG name.
        confirm: must be true to archive (a preview is returned otherwise).
    """
    from src.provisioning import Provisioner

    if not confirm:
        try:
            con = _warehouse_ro()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            return Provisioner(con, ensure=False).teardown(kg, confirm=False)
        finally:
            con.close()
    try:
        con = _warehouse_rw()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        prov = Provisioner(con, lock=_WRITE_LOCK)
        return prov.teardown(kg, confirm=True)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Read tools (free; read-only warehouse)
# --------------------------------------------------------------------------- #

@mcp.tool
def kg_status(kg: str) -> dict:
    """Entity/document/claim counts, bound-source health and ingest lag for a
    deployed KG, plus its recent lineage. Read-only and free.

    Args:
        kg: the KG name.
    """
    from src.provisioning import Provisioner

    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        return Provisioner(con, ensure=False).status(kg)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def kg_list(include_archived: bool = False) -> dict:
    """List provisioned KGs with their namespace counts.

    Args:
        include_archived: include torn-down (archived) KGs.
    """
    from src.provisioning import Provisioner

    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        return Provisioner(con, ensure=False).list_kgs(include_archived=include_archived)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def kg_lineage(kg: Optional[str] = None, limit: int = 50) -> dict:
    """The provisioning lineage event log (deploy / attach / ingest / teardown),
    newest first, for all KGs or one.

    Args:
        kg: optional KG name to scope to.
        limit: max events (1-200).
    """
    from src.provisioning import Provisioner

    limit = max(1, min(int(limit), 200))
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        return Provisioner(con, ensure=False).lineage(kg, limit=limit)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "kgs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": ["string", "null"]},
                        "status": {"type": "string"},
                        "source_count": {"type": "integer"},
                        "counts": {"type": "object"},
                        "sources": {"type": "array"},
                        "sample": {
                            "type": "object",
                            "properties": {
                                "documents": {"type": "array"},
                                "entities": {"type": "array"},
                                "claims": {"type": "array"},
                            },
                        },
                    },
                },
            },
            "count": {"type": "integer"},
        },
        "additionalProperties": True,
    },
)
def kg_view(kg: Optional[str] = None) -> dict:
    """The `provisioned_kg` panel view: deployed KGs with their scoped panel
    family (a sample of the namespace's documents, top entities and claims),
    their namespace counts, and the sources feeding each (with the selection
    rationale). With a ``kg`` argument, scopes to that one namespace.

    Args:
        kg: optional KG name to scope the view.
    """
    from src.provisioning import Provisioner

    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        return Provisioner(con, ensure=False).view(kg)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


if __name__ == "__main__":
    mcp.run()
