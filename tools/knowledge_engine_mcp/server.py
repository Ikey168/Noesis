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
_MAINTENANCE_CANCELLATIONS: dict[str, threading.Event] = {}


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


def _safe(operation, *, write: bool = False, required_scope: str | None = None):
    conn = None
    try:
        scopes = _context()[1]
        if required_scope and required_scope not in scopes and "operator" not in scopes:
            return {
                "ok": False,
                "error": {
                    "code": "unauthorized",
                    "message": f"{required_scope} scope is required",
                },
            }
        if write and "operator" not in scopes and required_scope not in scopes:
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
            "noesis-document-revision-v1",
            "noesis-document-change-set-v1",
            "noesis-document-generation-delta-v1",
            "noesis-document-delta-replay-v1",
            "noesis-derived-object-revision-v1",
            "noesis-derived-object-generation-v1",
            "noesis-derived-object-generation-delta-v1",
            "noesis-derived-object-replay-v1",
            "noesis-derived-object-lineage-v1",
            "noesis-research-snapshot-v1",
            "noesis-research-snapshot-token-v1",
            "noesis-epistemic-taxonomy-v1",
            "noesis-epistemic-assessment-v1",
            "noesis-epistemic-explanation-v1",
            "noesis-hypothesis-workspace-v1",
            "noesis-hypothesis-comparison-v1",
            "noesis-hypothesis-research-plan-v1",
            "noesis-hypothesis-export-v1",
            "noesis-source-identity-v1",
            "noesis-source-alias-decision-v1",
            "noesis-source-relationship-v1",
            "noesis-source-dossier-v1",
            "noesis-source-independence-v1",
            "noesis-maintenance-job-request-v1",
            "noesis-maintenance-job-receipt-v1",
            "noesis-knowledge-generation-v1",
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
            "immutable-document-revisions",
            "generation-deltas",
            "point-in-time-documents",
            "immutable-derived-object-revisions",
            "support-aware-truth-maintenance",
            "incremental-derived-projections",
            "snapshot-pinned-research-sessions",
            "snapshot-bound-query-cursors",
            "versioned-epistemic-status",
            "evidence-calibrated-assessments",
            "reviewed-epistemic-overrides",
            "versioned-hypothesis-workspaces",
            "independence-aware-hypothesis-comparison",
            "resumable-hypothesis-research-plans",
            "canonical-source-identities",
            "reversible-source-alias-resolution",
            "time-bounded-source-ownership-graph",
            "source-aware-evidence-independence",
            "knowledge-maintenance",
            "lease-safe-workers",
            "committed-generations",
            "unified-query",
            "temporal-query",
            "memory-context",
            "federated-query",
        ],
    }


@mcp.tool()
def document_revision(
    document_id: str,
    revision: int | None = None,
    generation: int | None = None,
    valid_at_ms: int | None = None,
    observed_before_ms: int | None = None,
    include_retracted: bool = False,
) -> dict:
    """Read one exact committed document revision using one temporal selector."""
    from src.ingestion.revisions import DocumentRevisionStore

    return _safe(
        lambda conn: {
            "ok": True,
            "revision": DocumentRevisionStore(conn, initialize=False).revision(
                document_id,
                revision=revision,
                generation=generation,
                valid_at=valid_at_ms,
                observed_before=observed_before_ms,
                include_retracted=include_retracted,
            ),
        },
        required_scope="knowledge:read",
    )


@mcp.tool()
def document_revision_history(document_id: str, include_retracted: bool = True) -> dict:
    """List immutable committed revisions for a document in lineage order."""
    from src.ingestion.revisions import DocumentRevisionStore

    return _safe(
        lambda conn: {
            "ok": True,
            "revisions": DocumentRevisionStore(conn, initialize=False).history(
                document_id, include_retracted=include_retracted
            ),
        },
        required_scope="knowledge:read",
    )


@mcp.tool()
def document_generation_delta(
    pack_id: str,
    from_watermark: int | None = None,
    to_watermark: int | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> dict:
    """Page deterministic changes across a committed source-pack watermark range."""
    from src.ingestion.revisions import DocumentRevisionStore

    return _safe(
        lambda conn: DocumentRevisionStore(conn, initialize=False).delta(
            pack_id,
            from_watermark=from_watermark,
            to_watermark=to_watermark,
            cursor=cursor,
            limit=limit,
        ),
        required_scope="knowledge:read",
    )


@mcp.tool()
def replay_document_generation_delta(
    pack_id: str, from_watermark: int, to_watermark: int
) -> dict:
    """Verify a committed delta deterministically against immutable revisions."""
    from src.ingestion.revisions import DocumentRevisionStore

    return _safe(
        lambda conn: DocumentRevisionStore(conn, initialize=False).replay(
            pack_id, from_watermark, to_watermark
        ),
        required_scope="knowledge:read",
    )


@mcp.tool()
def document_revision_health() -> dict:
    """Return text-free revision, uncommitted, and change-set health metrics."""
    from src.ingestion.revisions import DocumentRevisionStore

    return _safe(
        lambda conn: DocumentRevisionStore(conn, initialize=False).health(),
        required_scope="knowledge:read",
    )


@mcp.tool()
def derived_object_revision(
    namespace: str,
    logical_id: str,
    revision: int | None = None,
    generation: int | None = None,
    include_retracted: bool = False,
) -> dict:
    """Read one immutable derived-object revision at an optional generation."""
    from src.kb.derived_revisions import DerivedRevisionStore

    return _safe(
        lambda conn: {
            "ok": True,
            "revision": DerivedRevisionStore(conn, initialize=False).revision(
                namespace,
                logical_id,
                revision=revision,
                generation=generation,
                include_retracted=include_retracted,
            ),
        },
        required_scope="knowledge:read",
    )


@mcp.tool()
def derived_object_history(
    namespace: str, logical_id: str, include_retracted: bool = True
) -> dict:
    """List immutable revisions and support transitions for one derived object."""
    from src.kb.derived_revisions import DerivedRevisionStore

    return _safe(
        lambda conn: {
            "ok": True,
            "revisions": DerivedRevisionStore(conn, initialize=False).history(
                namespace, logical_id, include_retracted=include_retracted
            ),
        },
        required_scope="knowledge:read",
    )


@mcp.tool()
def derived_object_generation_delta(
    namespace: str,
    from_generation: int,
    to_generation: int,
    cursor: str | None = None,
    limit: int = 100,
) -> dict:
    """Page object-level additions, updates, support changes, and retractions."""
    from src.kb.derived_revisions import DerivedRevisionStore

    return _safe(
        lambda conn: DerivedRevisionStore(conn, initialize=False).delta(
            namespace,
            from_generation=from_generation,
            to_generation=to_generation,
            cursor=cursor,
            limit=limit,
        ),
        required_scope="knowledge:read",
    )


@mcp.tool()
def replay_derived_object_generations(
    namespace: str, from_generation: int, to_generation: int
) -> dict:
    """Verify derived-object deltas against immutable revisions and receipts."""
    from src.kb.derived_revisions import DerivedRevisionStore

    return _safe(
        lambda conn: DerivedRevisionStore(conn, initialize=False).replay(
            namespace, from_generation, to_generation
        ),
        required_scope="knowledge:read",
    )


@mcp.tool()
def derived_object_lineage(revision_id: str) -> dict:
    """Trace a derived revision to exact sources and current projections."""
    from src.kb.derived_revisions import DerivedRevisionStore

    return _safe(
        lambda conn: DerivedRevisionStore(conn, initialize=False).lineage(revision_id),
        required_scope="knowledge:read",
    )


@mcp.tool()
def explain_derived_object_invalidation(
    namespace: str, logical_id: str, generation: int | None = None
) -> dict:
    """Explain the document and support changes affecting a derived object."""
    from src.kb.derived_revisions import DerivedRevisionStore

    return _safe(
        lambda conn: DerivedRevisionStore(conn, initialize=False).explain_invalidation(
            namespace, logical_id, generation
        ),
        required_scope="knowledge:read",
    )


@mcp.tool()
def derived_projection(namespace: str, projection_kind: str) -> dict:
    """Read an atomically published lexical, vector, graph, or summary projection."""
    from src.kb.derived_revisions import DerivedRevisionStore

    return _safe(
        lambda conn: {
            "ok": True,
            "items": DerivedRevisionStore(conn, initialize=False).projection(
                namespace, projection_kind
            ),
        },
        required_scope="knowledge:read",
    )


@mcp.tool()
def derived_object_health() -> dict:
    """Return text-free derived revision, support, projection, and generation counts."""
    from src.kb.derived_revisions import DerivedRevisionStore

    return _safe(
        lambda conn: DerivedRevisionStore(conn, initialize=False).health(),
        required_scope="knowledge:read",
    )


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
def explain_knowledge_query(request: dict[str, Any], snapshot_token: str = "") -> dict:
    """Return the deterministic source, dependency, omission, and budget plan."""
    return _safe(
        lambda conn: _query_engine(conn, request).plan(
            _bind_snapshot(conn, request, snapshot_token), scopes=_context()[1]
        )
    )


@mcp.tool()
def query_knowledge(
    request: dict[str, Any], query_id: str = "", snapshot_token: str = ""
) -> dict:
    """Query authorized local knowledge with evidence-preserving unified results."""
    token = (
        query_id
        or "query:"
        + __import__("hashlib")
        .sha256(
            __import__("json")
            .dumps(
                {
                    "request": request,
                    "snapshot_token_hash": __import__("hashlib")
                    .sha256(snapshot_token.encode())
                    .hexdigest()
                    if snapshot_token
                    else None,
                },
                sort_keys=True,
                default=str,
            )
            .encode()
        )
        .hexdigest()[:24]
    )
    event = _QUERY_CANCELLATIONS.setdefault(token, threading.Event())

    def operation(conn):
        bound = _bind_snapshot(conn, request, snapshot_token)
        result = _query_engine(conn, bound).execute(
            bound, scopes=_context()[1], cancelled=event.is_set
        )
        _QUERY_REPLAYS[token] = {"request": bound, "result": result}
        return result

    try:
        return _safe(operation)
    finally:
        _QUERY_CANCELLATIONS.pop(token, None)


def _bind_snapshot(conn, request: dict[str, Any], token: str) -> dict[str, Any]:
    if not token:
        return request
    from src.kb.research_snapshots import ResearchSnapshotStore

    principal, scopes = _context()
    return ResearchSnapshotStore(conn, initialize=False).bind_query(
        token, request, principal_id=principal, scopes=scopes
    )


@mcp.tool()
def begin_research_snapshot(
    selection: dict[str, Any],
    ttl_ms: int = 3_600_000,
    maximum_lifetime_ms: int = 86_400_000,
) -> dict:
    """Begin a durable session pinned to one consistent knowledge vector."""
    from src.kb.research_snapshots import ResearchSnapshotStore

    principal, scopes = _context()
    return _safe(
        lambda conn: ResearchSnapshotStore(conn).begin(
            selection,
            principal_id=principal,
            scopes=scopes,
            ttl_ms=ttl_ms,
            maximum_lifetime_ms=maximum_lifetime_ms,
        ),
        write=True,
        required_scope="knowledge:snapshot:write",
    )


@mcp.tool()
def inspect_research_snapshot(token: str) -> dict:
    """Inspect a research snapshot without returning its bearer token."""
    from src.kb.research_snapshots import ResearchSnapshotStore

    principal, scopes = _context()
    return _safe(
        lambda conn: ResearchSnapshotStore(conn, initialize=False).inspect(
            token, principal_id=principal, scopes=scopes
        ),
        required_scope="knowledge:snapshot:read",
    )


@mcp.tool()
def renew_research_snapshot(token: str, ttl_ms: int) -> dict:
    """Renew a snapshot without exceeding its maximum lifetime."""
    from src.kb.research_snapshots import ResearchSnapshotStore

    principal, scopes = _context()
    return _safe(
        lambda conn: ResearchSnapshotStore(conn, initialize=False).renew(
            token, principal_id=principal, scopes=scopes, ttl_ms=ttl_ms
        ),
        write=True,
        required_scope="knowledge:snapshot:write",
    )


@mcp.tool()
def close_research_snapshot(token: str) -> dict:
    """Close a snapshot and release its retention pins."""
    from src.kb.research_snapshots import ResearchSnapshotStore

    principal, scopes = _context()
    return _safe(
        lambda conn: ResearchSnapshotStore(conn, initialize=False).close(
            token, principal_id=principal, scopes=scopes
        ),
        write=True,
        required_scope="knowledge:snapshot:write",
    )


@mcp.tool()
def research_snapshot_pins(token: str) -> dict:
    """List the generations protected by an active or historical snapshot."""
    from src.kb.research_snapshots import ResearchSnapshotStore

    principal, scopes = _context()
    return _safe(
        lambda conn: ResearchSnapshotStore(conn, initialize=False).pins(
            token, principal_id=principal, scopes=scopes
        ),
        required_scope="knowledge:snapshot:read",
    )


@mcp.tool()
def research_snapshot_health() -> dict:
    """Return session and active retention-pin counts."""
    from src.kb.research_snapshots import ResearchSnapshotStore

    return _safe(
        lambda conn: ResearchSnapshotStore(conn, initialize=False).health(),
        required_scope="knowledge:snapshot:read",
    )


@mcp.tool()
def classify_epistemic_statement(text: str) -> dict:
    """Classify a statement kind using deterministic, inspectable rules."""
    from src.kb.epistemic import classify_statement

    return _safe(
        lambda conn: {"ok": True, "classification": classify_statement(text)},
        required_scope="knowledge:epistemic:read",
    )


@mcp.tool()
def register_epistemic_taxonomy(
    name: str,
    semantic_version: str,
    definitions: dict[str, str],
    domain: str = "core",
    supersedes_taxonomy_id: str | None = None,
) -> dict:
    """Register an immutable core-compatible taxonomy or domain extension."""
    from src.kb.epistemic import EpistemicStore

    scopes = _context()[1]
    return _safe(
        lambda conn: EpistemicStore(conn).register_taxonomy(
            name,
            semantic_version,
            definitions,
            scopes=scopes,
            domain=domain,
            supersedes_taxonomy_id=supersedes_taxonomy_id,
        ),
        write=True,
        required_scope="knowledge:epistemic:write",
    )


@mcp.tool()
def list_epistemic_taxonomies(domain: str | None = None) -> dict:
    """List versioned epistemic taxonomies visible to the caller."""
    from src.kb.epistemic import EpistemicStore

    return _safe(
        lambda conn: {
            "ok": True,
            "items": EpistemicStore(conn, initialize=False).list_taxonomies(
                scopes=_context()[1], domain=domain
            ),
        },
        required_scope="knowledge:epistemic:read",
    )


@mcp.tool()
def assess_epistemic_statement(
    namespace: str,
    statement_id: str,
    text: str,
    evidence: list[dict[str, Any]],
    source_revision_id: str | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict:
    """Persist a versioned statement-kind and independent-evidence assessment."""
    from src.kb.epistemic import EpistemicStore

    principal, scopes = _context()
    return _safe(
        lambda conn: EpistemicStore(conn).assess(
            namespace,
            statement_id,
            text,
            evidence,
            principal_id=principal,
            scopes=scopes,
            source_revision_id=source_revision_id,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy=policy,
        ),
        write=True,
        required_scope="knowledge:epistemic:write",
    )


@mcp.tool()
def review_epistemic_status(
    namespace: str,
    statement_id: str,
    status: str,
    reason: str,
    expected_assessment_id: str | None = None,
) -> dict:
    """Record an authorized override without erasing machine assessment history."""
    from src.kb.epistemic import EpistemicStore

    principal, scopes = _context()
    return _safe(
        lambda conn: EpistemicStore(conn, initialize=False).override(
            namespace,
            statement_id,
            status,
            reason,
            reviewer_id=principal,
            scopes=scopes,
            expected_assessment_id=expected_assessment_id,
        ),
        write=True,
        required_scope="knowledge:epistemic:review",
    )


@mcp.tool()
def get_epistemic_assessment(
    namespace: str, statement_id: str, include_history: bool = False
) -> dict:
    """Read the current assessment or its immutable revision history."""
    from src.kb.epistemic import EpistemicStore

    return _safe(
        lambda conn: EpistemicStore(conn, initialize=False).get(
            namespace,
            statement_id,
            scopes=_context()[1],
            include_history=include_history,
        ),
        required_scope="knowledge:epistemic:read",
    )


@mcp.tool()
def search_epistemic_assessments(
    namespace: str,
    statuses: list[str] | None = None,
    assessment_states: list[str] | None = None,
    limit: int = 100,
) -> dict:
    """Filter current assessments by statement kind and evidence state."""
    from src.kb.epistemic import EpistemicStore

    def operation(conn):
        store = EpistemicStore(conn, initialize=False)
        items = store.search(
            namespace,
            scopes=_context()[1],
            statuses=statuses or [],
            states=assessment_states or [],
            limit=limit,
        )
        return {"ok": True, "items": items, "facets": store.aggregate(items)}

    return _safe(
        operation,
        required_scope="knowledge:epistemic:read",
    )


@mcp.tool()
def explain_epistemic_assessment(namespace: str, statement_id: str) -> dict:
    """Explain classification, evidence aggregation, uncertainty, and overrides."""
    from src.kb.epistemic import EpistemicStore

    return _safe(
        lambda conn: EpistemicStore(conn, initialize=False).explain(
            namespace, statement_id, scopes=_context()[1]
        ),
        required_scope="knowledge:epistemic:read",
    )


@mcp.tool()
def create_hypothesis_workspace(
    namespace: str,
    title: str,
    hypotheses: list[dict[str, Any]],
    idempotency_key: str | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict:
    """Create an idempotent, versioned competing-hypothesis workspace."""
    from src.kb.hypotheses import HypothesisStore

    principal, scopes = _context()
    return _safe(
        lambda conn: HypothesisStore(conn).create(
            namespace,
            title,
            hypotheses,
            principal_id=principal,
            scopes=scopes,
            idempotency_key=idempotency_key,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy=policy,
        ),
        write=True,
        required_scope="knowledge:hypothesis:write",
    )


@mcp.tool()
def get_hypothesis_workspace(
    namespace: str, workspace_id: str, include_history: bool = False
) -> dict:
    """Inspect the current workspace or its immutable revision history."""
    from src.kb.hypotheses import HypothesisStore

    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).get(
            namespace,
            workspace_id,
            scopes=_context()[1],
            include_history=include_history,
        ),
        required_scope="knowledge:hypothesis:read",
    )


@mcp.tool()
def revise_hypothesis_workspace(
    namespace: str,
    workspace_id: str,
    expected_revision: int,
    title: str | None = None,
    hypotheses: list[dict[str, Any]] | None = None,
    lifecycle: str | None = None,
) -> dict:
    """Write an optimistic-concurrency workspace revision."""
    from src.kb.hypotheses import HypothesisStore

    principal, scopes = _context()
    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).revise(
            namespace,
            workspace_id,
            principal_id=principal,
            scopes=scopes,
            expected_revision=expected_revision,
            title=title,
            hypotheses=hypotheses,
            lifecycle=lifecycle,
        ),
        write=True,
        required_scope="knowledge:hypothesis:write",
    )


@mcp.tool()
def branch_hypothesis_workspace(
    namespace: str,
    workspace_id: str,
    title: str,
    idempotency_key: str | None = None,
) -> dict:
    """Branch a workspace while retaining stable hypothesis identities."""
    from src.kb.hypotheses import HypothesisStore

    principal, scopes = _context()
    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).branch(
            namespace,
            workspace_id,
            title,
            principal_id=principal,
            scopes=scopes,
            idempotency_key=idempotency_key,
        ),
        write=True,
        required_scope="knowledge:hypothesis:write",
    )


@mcp.tool()
def retire_hypothesis_workspace(
    namespace: str, workspace_id: str, expected_revision: int
) -> dict:
    """Retire a workspace through a retained lifecycle revision."""
    from src.kb.hypotheses import HypothesisStore

    principal, scopes = _context()
    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).revise(
            namespace,
            workspace_id,
            principal_id=principal,
            scopes=scopes,
            expected_revision=expected_revision,
            lifecycle="retired",
        ),
        write=True,
        required_scope="knowledge:hypothesis:write",
    )


@mcp.tool()
def link_hypothesis_evidence(
    namespace: str,
    workspace_id: str,
    hypothesis_id: str,
    evidence_id: str,
    stance: str,
    source_revision_id: str | None = None,
    relevance: float = 1.0,
    independence_group: str | None = None,
    provenance: dict[str, Any] | None = None,
    annotations: dict[str, Any] | None = None,
    required_scope: str | None = None,
) -> dict:
    """Link provenance-rich evidence to a hypothesis with declared stance."""
    from src.kb.hypotheses import HypothesisStore

    principal, scopes = _context()
    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).link_evidence(
            namespace,
            workspace_id,
            hypothesis_id,
            evidence_id,
            stance,
            principal_id=principal,
            scopes=scopes,
            source_revision_id=source_revision_id,
            relevance=relevance,
            independence_group=independence_group,
            provenance=provenance,
            annotations=annotations,
            required_scope=required_scope,
        ),
        write=True,
        required_scope="knowledge:hypothesis:write",
    )


@mcp.tool()
def retract_hypothesis_evidence(
    namespace: str, workspace_id: str, link_id: str, reason: str
) -> dict:
    """Append a retraction revision to a hypothesis evidence link."""
    from src.kb.hypotheses import HypothesisStore

    principal, scopes = _context()
    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).retract_evidence(
            namespace,
            workspace_id,
            link_id,
            reason,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:hypothesis:write",
    )


@mcp.tool()
def compare_hypotheses(
    namespace: str,
    workspace_id: str,
    method: str = "qualitative",
    priors: dict[str, float] | None = None,
    sensitivity: float = 0.15,
) -> dict:
    """Compare hypotheses without representing heuristic scores as truth probabilities."""
    from src.kb.hypotheses import HypothesisStore

    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).compare(
            namespace,
            workspace_id,
            scopes=_context()[1],
            method=method,
            priors=priors,
            sensitivity=sensitivity,
        ),
        required_scope="knowledge:hypothesis:read",
    )


@mcp.tool()
def create_hypothesis_research_plan(
    namespace: str, workspace_id: str, max_steps: int = 25
) -> dict:
    """Create bounded checks for a workspace's discriminating predictions."""
    from src.kb.hypotheses import HypothesisStore

    principal, scopes = _context()
    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).create_plan(
            namespace,
            workspace_id,
            principal_id=principal,
            scopes=scopes,
            max_steps=max_steps,
        ),
        write=True,
        required_scope="knowledge:hypothesis:write",
    )


@mcp.tool()
def get_hypothesis_research_plan(namespace: str, plan_id: str) -> dict:
    """Inspect resumable plan progress and unresolved information gaps."""
    from src.kb.hypotheses import HypothesisStore

    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).get_plan(
            namespace, plan_id, scopes=_context()[1]
        ),
        required_scope="knowledge:hypothesis:read",
    )


@mcp.tool()
def execute_hypothesis_research_plan(
    namespace: str,
    plan_id: str,
    observations: list[dict[str, Any]],
    budget: float,
    cancel_requested: bool = False,
) -> dict:
    """Apply bounded observations to a plan, retaining a resumable cursor."""
    from src.kb.hypotheses import HypothesisStore

    principal, scopes = _context()
    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).execute_plan(
            namespace,
            plan_id,
            observations,
            principal_id=principal,
            scopes=scopes,
            budget=budget,
            cancel_requested=cancel_requested,
        ),
        write=True,
        required_scope="knowledge:hypothesis:execute",
    )


@mcp.tool()
def export_hypothesis_workspace(namespace: str, workspace_id: str) -> dict:
    """Export complete accessible workspace lineage with a deterministic hash."""
    from src.kb.hypotheses import HypothesisStore

    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).export(
            namespace, workspace_id, scopes=_context()[1]
        ),
        required_scope="knowledge:hypothesis:read",
    )


@mcp.tool()
def replay_hypothesis_workspace(namespace: str, workspace_id: str) -> dict:
    """Replay a hypothesis export and verify deterministic reconstruction."""
    from src.kb.hypotheses import HypothesisStore

    return _safe(
        lambda conn: HypothesisStore(conn, initialize=False).replay(
            namespace, workspace_id, scopes=_context()[1]
        ),
        required_scope="knowledge:hypothesis:read",
    )


@mcp.tool()
def register_source_identity(
    namespace: str,
    kind: str,
    display_name: str,
    native_ids: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
    idempotency_key: str | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict:
    """Register a canonical publication, organization, author, channel, or account."""
    from src.kb.source_identity import SourceIdentityStore

    principal, scopes = _context()
    return _safe(
        lambda conn: SourceIdentityStore(conn).register(
            namespace,
            kind,
            display_name,
            principal_id=principal,
            scopes=scopes,
            native_ids=native_ids,
            names=names,
            idempotency_key=idempotency_key,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy=policy,
        ),
        write=True,
        required_scope="knowledge:source-identity:write",
    )


@mcp.tool()
def lookup_source_identity(namespace: str, source_id: str) -> dict:
    """Look up the current canonical source revision."""
    from src.kb.source_identity import SourceIdentityStore

    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).get(
            namespace, source_id, scopes=_context()[1]
        ),
        required_scope="knowledge:source-identity:read",
    )


@mcp.tool()
def source_identity_history(namespace: str, source_id: str) -> dict:
    """Return immutable source identity revisions, including rename and deletion."""
    from src.kb.source_identity import SourceIdentityStore

    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).get(
            namespace, source_id, scopes=_context()[1], include_history=True
        ),
        required_scope="knowledge:source-identity:read",
    )


@mcp.tool()
def revise_source_identity(
    namespace: str,
    source_id: str,
    expected_revision: int,
    display_name: str | None = None,
    native_ids: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
) -> dict:
    """Append an optimistic-concurrency source identity revision."""
    from src.kb.source_identity import SourceIdentityStore

    principal, scopes = _context()
    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).revise(
            namespace,
            source_id,
            expected_revision,
            principal_id=principal,
            scopes=scopes,
            display_name=display_name,
            native_ids=native_ids,
            names=names,
        ),
        write=True,
        required_scope="knowledge:source-identity:write",
    )


@mcp.tool()
def delete_source_identity(
    namespace: str, source_id: str, expected_revision: int
) -> dict:
    """Record source/account deletion without erasing identity history."""
    from src.kb.source_identity import SourceIdentityStore

    principal, scopes = _context()
    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).revise(
            namespace,
            source_id,
            expected_revision,
            principal_id=principal,
            scopes=scopes,
            lifecycle="deleted",
        ),
        write=True,
        required_scope="knowledge:source-identity:write",
    )


@mcp.tool()
def decide_source_alias(
    namespace: str,
    source_id: str,
    alias_type: str,
    value: str,
    reason: str,
    language: str = "und",
    confidence: float = 1.0,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Record a reviewed URL, domain, handle, identifier, or multilingual alias."""
    from src.kb.source_identity import SourceIdentityStore

    principal, scopes = _context()
    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).decide_alias(
            namespace,
            source_id,
            alias_type,
            value,
            language=language,
            confidence=confidence,
            reason=reason,
            provenance=provenance,
            reviewer_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:source-identity:review",
    )


@mcp.tool()
def split_source_alias(
    namespace: str,
    source_id: str,
    alias_type: str,
    value: str,
    reason: str,
    language: str = "und",
) -> dict:
    """Reverse a reviewed alias merge through an append-only split decision."""
    from src.kb.source_identity import SourceIdentityStore

    principal, scopes = _context()
    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).decide_alias(
            namespace,
            source_id,
            alias_type,
            value,
            language=language,
            reason=reason,
            reviewer_id=principal,
            scopes=scopes,
            action="split",
        ),
        write=True,
        required_scope="knowledge:source-identity:review",
    )


@mcp.tool()
def resolve_source_alias(
    namespace: str,
    alias_type: str,
    value: str,
    language: str = "und",
    limit: int = 25,
) -> dict:
    """Resolve an alias while preserving ambiguous reviewed candidates."""
    from src.kb.source_identity import SourceIdentityStore

    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).resolve_alias(
            namespace,
            alias_type,
            value,
            scopes=_context()[1],
            language=language,
            limit=limit,
        ),
        required_scope="knowledge:source-identity:read",
    )


@mcp.tool()
def add_source_relationship(
    namespace: str,
    from_source_id: str,
    to_source_id: str,
    relationship_type: str,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    confidence: float = 1.0,
    uncertainty: float = 0.0,
    evidence: list[dict[str, Any]] | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict:
    """Add a sourced, time-bounded ownership, funding, control, or origin edge."""
    from src.kb.source_identity import SourceIdentityStore

    principal, scopes = _context()
    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).relate(
            namespace,
            from_source_id,
            to_source_id,
            relationship_type,
            principal_id=principal,
            scopes=scopes,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            confidence=confidence,
            uncertainty=uncertainty,
            evidence=evidence or [],
            producer=producer,
            policy=policy,
        ),
        write=True,
        required_scope="knowledge:source-identity:write",
    )


@mcp.tool()
def retract_source_relationship(
    namespace: str, relationship_id: str, reason: str
) -> dict:
    """Retract a source relationship without deleting its evidence history."""
    from src.kb.source_identity import SourceIdentityStore

    principal, scopes = _context()
    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).retract_relationship(
            namespace,
            relationship_id,
            reason,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:source-identity:write",
    )


@mcp.tool()
def source_identity_dossier(
    namespace: str,
    source_id: str,
    as_of_ms: int | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    """Return a paginated, citation-aware source dossier at a valid time."""
    from src.kb.source_identity import SourceIdentityStore

    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).dossier(
            namespace,
            source_id,
            scopes=_context()[1],
            as_of_ms=as_of_ms,
            limit=limit,
            cursor=cursor,
        ),
        required_scope="knowledge:source-identity:read",
    )


@mcp.tool()
def source_relationship_path(
    namespace: str,
    from_source_id: str,
    to_source_id: str,
    as_of_ms: int | None = None,
    max_depth: int = 6,
) -> dict:
    """Traverse a bounded ownership/control path at a requested valid time."""
    from src.kb.source_identity import SourceIdentityStore

    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).path(
            namespace,
            from_source_id,
            to_source_id,
            scopes=_context()[1],
            as_of_ms=as_of_ms,
            max_depth=max_depth,
        ),
        required_scope="knowledge:source-identity:read",
    )


@mcp.tool()
def explain_source_independence(
    namespace: str, source_ids: list[str], as_of_ms: int | None = None
) -> dict:
    """Explain evidence-independence groups from ownership, origin, and syndication."""
    from src.kb.source_identity import SourceIdentityStore

    return _safe(
        lambda conn: SourceIdentityStore(conn, initialize=False).explain_independence(
            namespace, source_ids, scopes=_context()[1], as_of_ms=as_of_ms
        ),
        required_scope="knowledge:source-identity:read",
    )


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


def _maintenance(conn, initialize: bool = False):
    from src.kb.maintenance import MaintenanceOrchestrator

    return MaintenanceOrchestrator(conn, root=ROOT, initialize=initialize)


def _run_maintenance(conn, owner_id: str, live_network: bool, max_jobs: int) -> dict:
    from src.kb.maintenance import fixture_adapter_provider

    orchestrator = _maintenance(conn, initialize=True)
    orchestrator.enqueue_due(
        principal_id=_context()[0], network="live" if live_network else "disabled"
    )
    due = orchestrator.due_work(limit=max_jobs)["jobs"]
    receipts = []
    for item in due:
        event = _MAINTENANCE_CANCELLATIONS.setdefault(item["job_id"], threading.Event())
        try:
            receipt = orchestrator.run_once(
                owner_id,
                principal_id=_context()[0],
                adapter_provider=None
                if live_network
                else fixture_adapter_provider(orchestrator),
                secret_resolver=_secret_resolver,
                dns_resolver=None if live_network else lambda _: ["8.8.8.8"],
                cancelled=event.is_set,
            )
            if receipt["status"] == "idle":
                break
            receipts.append(receipt)
        finally:
            _MAINTENANCE_CANCELLATIONS.pop(item["job_id"], None)
    return {
        "contract": "noesis-maintenance-drain-v1",
        "owner_id": owner_id,
        "jobs": receipts,
        "processed": len(receipts),
        "bounded": True,
    }


@mcp.tool()
def run_maintenance_once(
    owner_id: str = "mcp-maintenance-worker", live_network: bool = False
) -> dict:
    """Run at most one due source-pack maintenance job; fixtures are the safe default."""
    return _safe(
        lambda conn: _run_maintenance(conn, owner_id, live_network, 1), write=True
    )


@mcp.tool()
def run_maintenance_drain(
    owner_id: str = "mcp-maintenance-worker",
    max_jobs: int = 10,
    live_network: bool = False,
) -> dict:
    """Drain a bounded number of due knowledge-maintenance jobs."""
    return _safe(
        lambda conn: _run_maintenance(conn, owner_id, live_network, max_jobs),
        write=True,
    )


@mcp.tool()
def list_maintenance_jobs(limit: int = 100) -> dict:
    """List durable jobs, attempts, terminal states, and worker health."""
    return _safe(lambda conn: _maintenance(conn).status(limit=limit))


@mcp.tool()
def list_maintenance_due_work(limit: int = 100) -> dict:
    """List bounded runnable maintenance work without claiming it."""
    return _safe(lambda conn: _maintenance(conn).due_work(limit=limit))


@mcp.tool()
def inspect_maintenance_job(job_id: str) -> dict:
    """Inspect a maintenance job, fenced lease, and immutable attempt history."""
    return _safe(lambda conn: _maintenance(conn).inspect_job(job_id))


@mcp.tool()
def set_maintenance_schedule_paused(
    pack_id: str, paused: bool, reason: str = "operator"
) -> dict:
    """Pause or resume dispatch for one source pack without deleting its schedule."""
    return _safe(
        lambda conn: _maintenance(conn, initialize=True).set_schedule_paused(
            pack_id, paused, principal_id=_context()[0], reason=reason
        ),
        write=True,
        required_scope="knowledge:maintenance:admin",
    )


@mcp.tool()
def pause_maintenance_schedule(pack_id: str, reason: str = "operator") -> dict:
    """Pause dispatch for one source pack while preserving its durable schedule."""
    return _safe(
        lambda conn: _maintenance(conn, initialize=True).set_schedule_paused(
            pack_id, True, principal_id=_context()[0], reason=reason
        ),
        write=True,
        required_scope="knowledge:maintenance:admin",
    )


@mcp.tool()
def resume_maintenance_schedule(pack_id: str, reason: str = "operator") -> dict:
    """Resume dispatch for one source pack from its persisted next due window."""
    return _safe(
        lambda conn: _maintenance(conn, initialize=True).set_schedule_paused(
            pack_id, False, principal_id=_context()[0], reason=reason
        ),
        write=True,
        required_scope="knowledge:maintenance:admin",
    )


@mcp.tool()
def cancel_maintenance_job(job_id: str) -> dict:
    """Persist cancellation and signal an active in-process maintenance job."""
    event = _MAINTENANCE_CANCELLATIONS.get(job_id)
    if event is not None:
        event.set()
    return _safe(
        lambda conn: _maintenance(conn, initialize=True).cancel(
            job_id, principal_id=_context()[0]
        ),
        write=True,
        required_scope="knowledge:maintenance:admin",
    )


@mcp.tool()
def retry_maintenance_job(job_id: str) -> dict:
    """Requeue a failed, cancelled, or dead-letter maintenance job."""
    return _safe(
        lambda conn: _maintenance(conn, initialize=True).retry(
            job_id, principal_id=_context()[0]
        ),
        write=True,
        required_scope="knowledge:maintenance:admin",
    )


@mcp.tool()
def recover_stale_maintenance_jobs() -> dict:
    """Recover expired leases and abandon their unfinished attempt records."""
    return _safe(
        lambda conn: _maintenance(conn, initialize=True).recover_stale(
            principal_id=_context()[0]
        ),
        write=True,
        required_scope="knowledge:maintenance:admin",
    )


@mcp.tool()
def inspect_maintenance_generation(generation_id: str) -> dict:
    """Inspect one append-only committed knowledge generation."""
    return _safe(lambda conn: _maintenance(conn).inspect_generation(generation_id))


@mcp.tool()
def replay_maintenance_generation(generation_id: str) -> dict:
    """Verify a generation against source receipts, workflow state, and event log."""
    return _safe(lambda conn: _maintenance(conn).replay_generation(generation_id))


@mcp.tool()
def maintenance_generation_lineage(generation_id: str) -> dict:
    """Trace a committed generation to pack, source runs, workflow, and artifacts."""
    return _safe(lambda conn: _maintenance(conn).generation_lineage(generation_id))


@mcp.tool()
def maintenance_health() -> dict:
    """Report stale leases, failures, schedule lag, and last committed generation."""
    return _safe(lambda conn: _maintenance(conn).health())


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
