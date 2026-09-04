"""MCP controls for residual knowledge-engine ingestion and derivation capabilities."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
mcp = FastMCP("noesis-knowledge-engine")
_QUERY_CANCELLATIONS: dict[str, threading.Event] = {}
_QUERY_REPLAYS: dict[str, dict[str, Any]] = {}
_SOURCE_PACK_CANCELLATIONS: dict[str, threading.Event] = {}


def _context() -> tuple[str, set[str]]:
    from src.config.env import resolve_env

    principal = (resolve_env("MCP_PRINCIPAL", "local-reader") or "").strip()
    raw = resolve_env("MCP_SCOPES", "knowledge:read") or ""
    return principal, {value.strip() for value in raw.split(",") if value.strip()}


def _connection(*, read_only: bool):
    import duckdb

    from src.config.env import warehouse_path

    return duckdb.connect(
        warehouse_path() or str(ROOT / "data/neuronews.duckdb"), read_only=read_only
    )


def _safe(operation, *, write: bool = False):
    conn = None
    try:
        if write and "operator" not in _context()[1]:
            return {
                "ok": False,
                "error": {
                    "code": "unauthorized",
                    "message": "operator scope is required",
                },
            }
        conn = _connection(read_only=not write)
        return operation(conn)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": {
                "code": getattr(exc, "code", "knowledge_engine_unavailable"),
                "message": str(exc)[:300],
            },
        }
    finally:
        if conn is not None:
            conn.close()


@mcp.tool()
def knowledge_engine_capabilities() -> dict:
    """Describe declarative ingestion, extraction, events, and artifact rebuild contracts."""
    return {
        "contracts": [
            "noesis-declarative-api-source-v1",
            "noesis-extractor-definition-v1",
            "noesis-canonical-event-v1",
            "noesis-derived-artifact-v1",
            "noesis-knowledge-workflow-v1",
            "noesis-workflow-stage-receipt-v1",
            "noesis-workflow-watermark-v1",
            "noesis-source-pack-v1",
            "noesis-source-pack-run-request-v1",
            "noesis-source-pack-run-receipt-v1",
            "noesis-knowledge-query-request-v1",
            "noesis-knowledge-query-plan-v1",
            "noesis-knowledge-query-result-v1",
        ],
        "features": [
            "declarative-rest",
            "versioned-extractors",
            "canonical-events",
            "selective-rebuild",
            "reference-workflow",
            "committed-watermarks",
            "production-source-packs",
            "source-pack-runtime",
            "source-pack-schedules",
            "source-pack-replay",
            "unified-query",
            "temporal-query",
            "memory-context",
            "federated-query",
        ],
    }


def _query_engine(conn, request: dict[str, Any]):
    from src.kb.unified_query import UnifiedQueryEngine, build_local_catalog

    scope = dict(request.get("scope") or {})
    principal, _ = _context()
    catalog = build_local_catalog(
        conn,
        domains=list(scope.get("domains") or ()),
        namespaces=list(scope.get("namespaces") or ()),
        tenant_id=scope.get("tenant_id"),
        task_id=scope.get("task_id"),
        principal_id=principal,
        include_memory=str(dict(request.get("memory") or {}).get("mode") or "off")
        != "off",
    )
    return UnifiedQueryEngine(catalog)


@mcp.tool()
def unified_query_capabilities(request: dict[str, Any]) -> dict:
    """Discover authorized local query capabilities for an intended scope."""
    return _safe(
        lambda conn: {
            "contract": "noesis-knowledge-query-capabilities-v1",
            "sources": _query_engine(conn, request).catalog.capabilities(
                scopes=_context()[1]
            ),
        }
    )


@mcp.tool()
def explain_knowledge_query(request: dict[str, Any]) -> dict:
    """Return the deterministic source, dependency, omission, and budget plan."""
    return _safe(
        lambda conn: _query_engine(conn, request).plan(request, scopes=_context()[1])
    )


@mcp.tool()
def query_knowledge(request: dict[str, Any], query_id: str = "") -> dict:
    """Query authorized local knowledge with evidence-preserving unified results."""
    token = (
        query_id
        or "query:"
        + __import__("hashlib")
        .sha256(__import__("json").dumps(request, sort_keys=True, default=str).encode())
        .hexdigest()[:24]
    )
    event = _QUERY_CANCELLATIONS.setdefault(token, threading.Event())

    def operation(conn):
        result = _query_engine(conn, request).execute(
            request, scopes=_context()[1], cancelled=event.is_set
        )
        _QUERY_REPLAYS[token] = {"request": request, "result": result}
        return result

    try:
        return _safe(operation)
    finally:
        _QUERY_CANCELLATIONS.pop(token, None)


@mcp.tool()
def cancel_knowledge_query(query_id: str) -> dict:
    """Signal cancellation for an in-process unified query."""
    event = _QUERY_CANCELLATIONS.get(query_id)
    if event is None:
        return {
            "ok": False,
            "error": {
                "code": "query_not_running",
                "message": "query is not running in this process",
            },
        }
    event.set()
    return {"ok": True, "query_id": query_id, "cancelled": True}


@mcp.tool()
def replay_knowledge_query(query_id: str) -> dict:
    """Replay a completed in-process query and compare its deterministic hash."""
    prior = _QUERY_REPLAYS.get(query_id)
    if prior is None:
        return {
            "ok": False,
            "error": {
                "code": "replay_not_found",
                "message": "query replay data is not available in this process",
            },
        }
    return _safe(
        lambda conn: _query_engine(conn, prior["request"]).replay(
            prior["request"], prior["result"], scopes=_context()[1]
        )
    )


@mcp.tool()
def evaluate_knowledge_query(
    result: dict[str, Any], expected_ids: list[str] | None = None
) -> dict:
    """Measure recall, citation coverage, provenance, failures, and memory separation."""
    from src.kb.unified_query import UnifiedQueryEngine

    return UnifiedQueryEngine.evaluate(result, expected_ids=expected_ids or [])


@mcp.tool()
def validate_knowledge_workflow(manifest: dict[str, Any]) -> dict:
    """Validate and hash a workflow manifest without executing it."""
    from src.kb.workflows import WorkflowError, validate_manifest

    try:
        return {"ok": True, "manifest": validate_manifest(manifest)}
    except WorkflowError as exc:
        return {"ok": False, "error": exc.as_dict()}


@mcp.tool()
def run_reference_workflow(
    documents: list[dict[str, Any]], run_key: str, namespace: str = "reference"
) -> dict:
    """Run or resume the canonical seven-stage Knowledge Engine workflow."""
    from src.kb.workflows import WorkflowStore, reference_handlers, reference_manifest

    def operation(conn):
        store = WorkflowStore(conn)
        return store.execute(
            reference_manifest(namespace),
            reference_handlers(conn, principal_id=_context()[0]),
            {"documents": documents},
            run_key=run_key,
        )

    return _safe(operation, write=True)


@mcp.tool()
def inspect_reference_workflow(run_id: str) -> dict:
    """Inspect workflow state, immutable receipts, and its committed watermark."""
    from src.kb.workflows import WorkflowStore

    return _safe(lambda conn: WorkflowStore(conn, initialize=False).inspect(run_id))


@mcp.tool()
def read_workflow_stage(
    namespace: str, workflow_id: str, watermark: int, stage: str
) -> dict:
    """Read a stage output from exactly one committed workflow generation."""
    from src.kb.workflows import WorkflowStore

    return _safe(
        lambda conn: WorkflowStore(conn, initialize=False).read_stage(
            namespace, workflow_id, watermark, stage
        )
    )


@mcp.tool()
def validate_source_pack(manifest: dict[str, Any]) -> dict:
    """Validate and content-address a deployable source-pack manifest."""
    from src.ingestion.source_packs import SourcePackError
    from src.ingestion.source_packs import validate_source_pack as validate

    try:
        return {"ok": True, "pack": validate(manifest)}
    except SourcePackError as exc:
        return {"ok": False, "error": exc.as_dict()}


@mcp.tool()
def install_source_pack(manifest: dict[str, Any], enable: bool = False) -> dict:
    """Install or upgrade an immutable source-pack version."""
    from src.ingestion.source_packs import SourcePackStore

    return _safe(
        lambda conn: SourcePackStore(conn).install(
            manifest, principal_id=_context()[0], enable=enable
        ),
        write=True,
    )


@mcp.tool()
def set_source_pack_enabled(pack_id: str, enabled: bool) -> dict:
    """Enable or disable one installed source pack without affecting others."""
    from src.ingestion.source_packs import SourcePackStore

    return _safe(
        lambda conn: SourcePackStore(conn, initialize=False).set_enabled(
            pack_id, enabled, principal_id=_context()[0]
        ),
        write=True,
    )


@mcp.tool()
def list_source_packs() -> dict:
    """List installed source-pack versions, readiness, and health."""
    from src.ingestion.source_packs import SourcePackStore

    return _safe(lambda conn: {"packs": SourcePackStore(conn, initialize=False).list()})


@mcp.tool()
def source_pack_coverage() -> dict:
    """Summarize configured, ready, and healthy sources by domain."""
    from src.ingestion.source_packs import SourcePackStore

    return _safe(lambda conn: SourcePackStore(conn, initialize=False).coverage())


def _source_pack_runtime(conn, initialize: bool = False):
    from src.ingestion.source_pack_runtime import SourcePackRuntime

    return SourcePackRuntime(conn, initialize=initialize)


def _secret_resolver(name: str) -> str | None:
    from src.config.env import resolve_env

    if not name.startswith("NOESIS_"):
        return None
    return resolve_env(name)


@mcp.tool()
def accept_source_pack_license(
    pack_id: str, source_id: str, redistribution: bool = False
) -> dict:
    """Record operator acceptance of the installed source terms and redistribution policy."""
    return _safe(
        lambda conn: _source_pack_runtime(conn, initialize=True).accept_license(
            pack_id,
            source_id,
            principal_id=_context()[0],
            redistribution=redistribution,
        ),
        write=True,
    )


@mcp.tool()
def preflight_source_pack_run(request: dict[str, Any]) -> dict:
    """Check immutable manifest, credentials, terms, network policy, and circuit readiness."""
    return _safe(
        lambda conn: _source_pack_runtime(conn).preflight(
            request, secret_available=lambda name: bool(_secret_resolver(name))
        )
    )


@mcp.tool()
def run_source_pack_execution(
    request: dict[str, Any], live_network: bool = False
) -> dict:
    """Run or resume one bounded source-pack execution; network access is explicit."""
    requested = dict(request)
    requested["network"] = "live" if live_network else "disabled"

    def operation(conn):
        runtime = _source_pack_runtime(conn, initialize=True)
        preflight = runtime.preflight(
            requested,
            secret_available=lambda name: bool(_secret_resolver(name)),
            dns_resolver=None if live_network else lambda _: ["8.8.8.8"],
        )
        event = _SOURCE_PACK_CANCELLATIONS.setdefault(
            preflight["run_id"], threading.Event()
        )
        try:
            return runtime.run(
                requested,
                principal_id=_context()[0],
                adapters=None
                if live_network
                else runtime.fixture_adapters(requested["pack_id"], ROOT),
                secret_resolver=_secret_resolver,
                dns_resolver=None if live_network else lambda _: ["8.8.8.8"],
                cancelled=event.is_set,
            )
        finally:
            _SOURCE_PACK_CANCELLATIONS.pop(preflight["run_id"], None)

    return _safe(operation, write=True)


@mcp.tool()
def cancel_source_pack_run(run_id: str) -> dict:
    """Cooperatively cancel an in-process source-pack run."""
    if "operator" not in _context()[1]:
        return {
            "ok": False,
            "error": {
                "code": "unauthorized",
                "message": "operator scope is required",
            },
        }
    event = _SOURCE_PACK_CANCELLATIONS.get(run_id)
    if event is None:
        return {
            "ok": False,
            "error": {
                "code": "run_not_active",
                "message": "source-pack run is not active in this process",
            },
        }
    event.set()
    return {"ok": True, "run_id": run_id, "cancelled": True}


@mcp.tool()
def inspect_source_pack_run(run_id: str) -> dict:
    """Inspect durable run state, per-source checkpoints, counts, and receipts."""
    return _safe(lambda conn: _source_pack_runtime(conn).inspect(run_id))


@mcp.tool()
def replay_source_pack_run(run_id: str) -> dict:
    """Recompute stable receipt identity for a completed source-pack run."""
    return _safe(lambda conn: _source_pack_runtime(conn).replay(run_id))


@mcp.tool()
def list_source_pack_quarantine(
    pack_id: str = "", run_id: str = "", state: str = "pending"
) -> dict:
    """List credential-safe quarantined record identities and mapping failures."""
    return _safe(
        lambda conn: _source_pack_runtime(conn).quarantine(
            pack_id=pack_id or None, run_id=run_id or None, state=state
        )
    )


@mcp.tool()
def retry_source_pack_quarantine(quarantine_ids: list[str]) -> dict:
    """Retry selected quarantined records against their exact installed mapping version."""
    return _safe(
        lambda conn: _source_pack_runtime(conn, initialize=True).retry_quarantine(
            quarantine_ids, principal_id=_context()[0]
        ),
        write=True,
    )


@mcp.tool()
def set_source_pack_schedule(
    pack_id: str, schedule: dict[str, Any], enabled: bool = True
) -> dict:
    """Create or replace a bounded credential-free interval schedule."""
    return _safe(
        lambda conn: _source_pack_runtime(conn, initialize=True).set_schedule(
            pack_id, schedule, principal_id=_context()[0], enabled=enabled
        ),
        write=True,
    )


@mcp.tool()
def list_source_pack_schedules(due_at_ms: int | None = None) -> dict:
    """List source-pack schedules and whether each is due."""
    return _safe(lambda conn: _source_pack_runtime(conn).schedules(due_at_ms=due_at_ms))


@mcp.tool()
def source_pack_runtime_coverage() -> dict:
    """Report execution, quarantine, degradation, and watermark coverage by domain."""
    return _safe(lambda conn: _source_pack_runtime(conn).runtime_coverage())


@mcp.tool()
def validate_api_connector_manifest(manifest: dict[str, Any]) -> dict:
    """Validate an API manifest without making a network request."""
    from src.ingestion.connectors.rest import (
        DeclarativeAPIConnector,
        DeclarativeAPIError,
    )

    # Validation at the MCP boundary deliberately does not resolve arbitrary DNS.
    try:
        host = manifest.get("allowed_hosts", [""])[0]
        return DeclarativeAPIConnector.validate_manifest(
            manifest,
            dns_resolver=lambda candidate: ["8.8.8.8"] if candidate == host else [],
        )
    except (DeclarativeAPIError, IndexError) as exc:
        return {
            "ok": False,
            "error": {
                "code": getattr(exc, "code", "invalid_manifest"),
                "message": str(exc),
            },
        }


@mcp.tool()
def fetch_declarative_api(
    manifest: dict[str, Any],
    operation_id: str,
    parameters: dict[str, Any] | None = None,
) -> dict:
    """Fetch and map one approved bounded GET operation without persisting it."""
    from src.ingestion.connectors.rest import (
        DeclarativeAPIConnector,
        DeclarativeAPIError,
    )

    try:
        connector = DeclarativeAPIConnector(manifest)
        values = connector.run(operation_id, parameters)
        return {
            "source": connector.describe()["source_id"],
            "items": [
                value.to_dict() if hasattr(value, "to_dict") else value
                for value in values
            ],
        }
    except DeclarativeAPIError as exc:
        return {"items": [], "error": {"code": exc.code, "message": exc.message}}


@mcp.tool()
def list_extractors() -> dict:
    """List immutable extractor versions and their declared capabilities."""
    from src.kb.extractors import ExtractorRegistry

    return _safe(
        lambda conn: {"extractors": ExtractorRegistry(conn, initialize=False).list()}
    )


@mcp.tool()
def register_extractor(definition: dict[str, Any]) -> dict:
    """Register extractor metadata; executable implementations remain operator-installed."""
    from src.kb.extractors import ExtractorRegistry

    return _safe(lambda conn: ExtractorRegistry(conn).register(definition), write=True)


@mcp.tool()
def plan_extractor_reprocessing(
    name: str,
    target_extractor_id: str,
    namespace: str,
    input_ids: list[str] | None = None,
) -> dict:
    """Preview selective side-by-side reprocessing without overwriting prior output."""
    from src.kb.extractors import ExtractorRegistry

    return _safe(
        lambda conn: ExtractorRegistry(conn, initialize=False).plan_reprocessing(
            name, target_extractor_id, namespace, input_ids=input_ids
        )
    )


@mcp.tool()
def resolve_event_report(
    namespace: str, report: dict[str, Any], report_id: str, auto_link: bool = True
) -> dict:
    """Resolve a report to a canonical event with confidence and alternatives."""
    from src.kb.events import EventResolver

    return _safe(
        lambda conn: EventResolver(conn).resolve_report(
            namespace, report, report_id=report_id, auto_link=auto_link
        ),
        write=True,
    )


@mcp.tool()
def merge_canonical_events(event_ids: list[str], reason: str) -> dict:
    """Merge explicitly selected events into one reversible canonical revision."""
    from src.kb.events import EventResolver

    return _safe(
        lambda conn: EventResolver(conn).merge(event_ids, reason=reason), write=True
    )


@mcp.tool()
def reverse_event_merge(operation_id: str, reason: str) -> dict:
    """Reverse a prior event merge and restore report links."""
    from src.kb.events import EventResolver

    return _safe(
        lambda conn: EventResolver(conn).reverse(operation_id, reason=reason),
        write=True,
    )


@mcp.tool()
def artifact_lineage(artifact_id: str, direction: str = "upstream") -> dict:
    """Traverse exact source, parser, extractor, schema, model, and config dependencies."""
    from src.kb.artifacts import ArtifactGraph

    return _safe(
        lambda conn: (
            ArtifactGraph(conn, initialize=False).upstream(artifact_id)
            if direction == "upstream"
            else ArtifactGraph(conn, initialize=False).downstream(artifact_id)
        )
    )


@mcp.tool()
def register_derived_artifact(artifact: dict[str, Any]) -> dict:
    """Register a stable derived-artifact generation and every known dependency edge."""
    from src.kb.artifacts import ArtifactGraph

    return _safe(
        lambda conn: ArtifactGraph(conn).register(
            artifact["namespace"],
            artifact["kind"],
            artifact["logical_id"],
            artifact.get("content"),
            configuration=artifact["configuration"],
            producer=artifact["producer"],
            dependencies=artifact["dependencies"],
            lineage_complete=artifact.get("lineage_complete", True),
        ),
        write=True,
    )


@mcp.tool()
def preview_artifact_invalidation(
    namespace: str, changes: list[dict[str, Any]]
) -> dict:
    """Preview dependency-ordered selective invalidation without side effects."""
    from src.kb.artifacts import ArtifactGraph

    return _safe(
        lambda conn: ArtifactGraph(conn, initialize=False).preview_invalidation(
            namespace, changes
        )
    )


@mcp.tool()
def execute_artifact_rebuild(
    plan: dict[str, Any], replacement_content: dict[str, Any], max_concurrency: int = 4
) -> dict:
    """Checkpoint rebuilds and atomically publish one consistent generation watermark."""
    from src.kb.artifacts import ArtifactGraph

    graph_holder = {}

    def operation(conn):
        graph = ArtifactGraph(conn)
        graph_holder["graph"] = graph
        builders = {
            kind: (
                lambda old, kind=kind: replacement_content.get(
                    old["artifact_id"], old["content"]
                )
            )
            for kind in [
                "source",
                "chunk",
                "enrichment",
                "embedding",
                "claim",
                "entity",
                "relation",
                "summary",
                "index",
                "bundle",
            ]
        }
        return graph.rebuild(plan, builders, max_concurrency=max_concurrency)

    return _safe(operation, write=True)


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)
