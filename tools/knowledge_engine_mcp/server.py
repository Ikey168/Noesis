"""MCP controls for residual knowledge-engine ingestion and derivation capabilities."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
mcp=FastMCP("noesis-knowledge-engine")


def _context() -> tuple[str,set[str]]:
    from src.config.env import resolve_env
    principal=(resolve_env("MCP_PRINCIPAL","local-reader") or "").strip();raw=resolve_env("MCP_SCOPES","knowledge:read") or ""
    return principal,{value.strip() for value in raw.split(",") if value.strip()}


def _connection(*,read_only: bool):
    import duckdb

    from src.config.env import warehouse_path
    return duckdb.connect(warehouse_path() or str(ROOT/"data/neuronews.duckdb"),read_only=read_only)


def _safe(operation,*,write: bool=False):
    conn=None
    try:
        if write and "operator" not in _context()[1]:
            return {"ok":False,"error":{"code":"unauthorized","message":"operator scope is required"}}
        conn=_connection(read_only=not write);return operation(conn)
    except Exception as exc:  # noqa: BLE001
        return {"ok":False,"error":{"code":getattr(exc,"code","knowledge_engine_unavailable"),"message":str(exc)[:300]}}
    finally:
        if conn is not None:conn.close()


@mcp.tool()
def knowledge_engine_capabilities() -> dict:
    """Describe declarative ingestion, extraction, events, and artifact rebuild contracts."""
    return {"contracts":["noesis-declarative-api-source-v1","noesis-extractor-definition-v1","noesis-canonical-event-v1","noesis-derived-artifact-v1","noesis-knowledge-workflow-v1","noesis-workflow-stage-receipt-v1","noesis-workflow-watermark-v1"],"features":["declarative-rest","versioned-extractors","canonical-events","selective-rebuild","reference-workflow","committed-watermarks"]}


@mcp.tool()
def validate_knowledge_workflow(manifest: dict[str,Any]) -> dict:
    """Validate and hash a workflow manifest without executing it."""
    from src.kb.workflows import WorkflowError,validate_manifest
    try:return {"ok":True,"manifest":validate_manifest(manifest)}
    except WorkflowError as exc:return {"ok":False,"error":exc.as_dict()}


@mcp.tool()
def run_reference_workflow(documents: list[dict[str,Any]],run_key: str,namespace: str="reference") -> dict:
    """Run or resume the canonical seven-stage Knowledge Engine workflow."""
    from src.kb.workflows import WorkflowStore,reference_handlers,reference_manifest
    def operation(conn):
        store=WorkflowStore(conn);return store.execute(reference_manifest(namespace),reference_handlers(conn,principal_id=_context()[0]),{"documents":documents},run_key=run_key)
    return _safe(operation,write=True)


@mcp.tool()
def inspect_reference_workflow(run_id: str) -> dict:
    """Inspect workflow state, immutable receipts, and its committed watermark."""
    from src.kb.workflows import WorkflowStore
    return _safe(lambda conn:WorkflowStore(conn,initialize=False).inspect(run_id))


@mcp.tool()
def read_workflow_stage(namespace: str,workflow_id: str,watermark: int,stage: str) -> dict:
    """Read a stage output from exactly one committed workflow generation."""
    from src.kb.workflows import WorkflowStore
    return _safe(lambda conn:WorkflowStore(conn,initialize=False).read_stage(namespace,workflow_id,watermark,stage))


@mcp.tool()
def validate_api_connector_manifest(manifest: dict[str,Any]) -> dict:
    """Validate an API manifest without making a network request."""
    from src.ingestion.connectors.rest import (
        DeclarativeAPIConnector,
        DeclarativeAPIError,
    )
    # Validation at the MCP boundary deliberately does not resolve arbitrary DNS.
    try:
        host=manifest.get("allowed_hosts",[""])[0]
        return DeclarativeAPIConnector.validate_manifest(manifest,dns_resolver=lambda candidate:["8.8.8.8"] if candidate==host else [])
    except (DeclarativeAPIError,IndexError) as exc:
        return {"ok":False,"error":{"code":getattr(exc,"code","invalid_manifest"),"message":str(exc)}}


@mcp.tool()
def fetch_declarative_api(manifest: dict[str,Any],operation_id: str,parameters: dict[str,Any] | None=None) -> dict:
    """Fetch and map one approved bounded GET operation without persisting it."""
    from src.ingestion.connectors.rest import (
        DeclarativeAPIConnector,
        DeclarativeAPIError,
    )
    try:
        connector=DeclarativeAPIConnector(manifest)
        values=connector.run(operation_id,parameters)
        return {"source":connector.describe()["source_id"],"items":[value.to_dict() if hasattr(value,"to_dict") else value for value in values]}
    except DeclarativeAPIError as exc:
        return {"items":[],"error":{"code":exc.code,"message":exc.message}}


@mcp.tool()
def list_extractors() -> dict:
    """List immutable extractor versions and their declared capabilities."""
    from src.kb.extractors import ExtractorRegistry
    return _safe(lambda conn:{"extractors":ExtractorRegistry(conn,initialize=False).list()})


@mcp.tool()
def register_extractor(definition: dict[str,Any]) -> dict:
    """Register extractor metadata; executable implementations remain operator-installed."""
    from src.kb.extractors import ExtractorRegistry
    return _safe(lambda conn:ExtractorRegistry(conn).register(definition),write=True)


@mcp.tool()
def plan_extractor_reprocessing(name: str,target_extractor_id: str,namespace: str,input_ids: list[str] | None=None) -> dict:
    """Preview selective side-by-side reprocessing without overwriting prior output."""
    from src.kb.extractors import ExtractorRegistry
    return _safe(lambda conn:ExtractorRegistry(conn,initialize=False).plan_reprocessing(name,target_extractor_id,namespace,input_ids=input_ids))


@mcp.tool()
def resolve_event_report(namespace: str,report: dict[str,Any],report_id: str,auto_link: bool=True) -> dict:
    """Resolve a report to a canonical event with confidence and alternatives."""
    from src.kb.events import EventResolver
    return _safe(lambda conn:EventResolver(conn).resolve_report(namespace,report,report_id=report_id,auto_link=auto_link),write=True)


@mcp.tool()
def merge_canonical_events(event_ids: list[str],reason: str) -> dict:
    """Merge explicitly selected events into one reversible canonical revision."""
    from src.kb.events import EventResolver
    return _safe(lambda conn:EventResolver(conn).merge(event_ids,reason=reason),write=True)


@mcp.tool()
def reverse_event_merge(operation_id: str,reason: str) -> dict:
    """Reverse a prior event merge and restore report links."""
    from src.kb.events import EventResolver
    return _safe(lambda conn:EventResolver(conn).reverse(operation_id,reason=reason),write=True)


@mcp.tool()
def artifact_lineage(artifact_id: str,direction: str="upstream") -> dict:
    """Traverse exact source, parser, extractor, schema, model, and config dependencies."""
    from src.kb.artifacts import ArtifactGraph
    return _safe(lambda conn:ArtifactGraph(conn,initialize=False).upstream(artifact_id) if direction=="upstream" else ArtifactGraph(conn,initialize=False).downstream(artifact_id))


@mcp.tool()
def register_derived_artifact(artifact: dict[str,Any]) -> dict:
    """Register a stable derived-artifact generation and every known dependency edge."""
    from src.kb.artifacts import ArtifactGraph
    return _safe(lambda conn:ArtifactGraph(conn).register(artifact["namespace"],artifact["kind"],artifact["logical_id"],artifact.get("content"),configuration=artifact["configuration"],producer=artifact["producer"],dependencies=artifact["dependencies"],lineage_complete=artifact.get("lineage_complete",True)),write=True)


@mcp.tool()
def preview_artifact_invalidation(namespace: str,changes: list[dict[str,Any]]) -> dict:
    """Preview dependency-ordered selective invalidation without side effects."""
    from src.kb.artifacts import ArtifactGraph
    return _safe(lambda conn:ArtifactGraph(conn,initialize=False).preview_invalidation(namespace,changes))


@mcp.tool()
def execute_artifact_rebuild(plan: dict[str,Any],replacement_content: dict[str,Any],max_concurrency: int=4) -> dict:
    """Checkpoint rebuilds and atomically publish one consistent generation watermark."""
    from src.kb.artifacts import ArtifactGraph
    graph_holder={}
    def operation(conn):
        graph=ArtifactGraph(conn);graph_holder["graph"]=graph
        builders={kind:(lambda old,kind=kind:replacement_content.get(old["artifact_id"],old["content"])) for kind in ["source","chunk","enrichment","embedding","claim","entity","relation","summary","index","bundle"]}
        return graph.rebuild(plan,builders,max_concurrency=max_concurrency)
    return _safe(operation,write=True)


if __name__=="__main__":
    from src.mcp_host.transport import run_server
    run_server(mcp)
