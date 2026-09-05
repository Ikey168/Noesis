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
_SOURCE_PLAN_CANCELLATIONS: dict[str, threading.Event] = {}
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
            "noesis-research-project-v1",
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
            "noesis-event-record-v2",
            "noesis-event-mention-v1",
            "noesis-event-account-v1",
            "noesis-event-relation-v1",
            "noesis-event-search-v1",
            "noesis-quantitative-metric-v1",
            "noesis-quantitative-observation-v1",
            "noesis-quantitative-calculation-v1",
            "noesis-quantitative-comparability-v1",
            "noesis-geospatial-place-v1",
            "noesis-geospatial-geometry-v1",
            "noesis-geocode-resolution-v1",
            "noesis-spatial-result-v1",
            "noesis-claim-state-v1",
            "noesis-claim-lineage-v1",
            "noesis-claim-successor-match-v1",
            "noesis-claim-timeline-v1",
            "noesis-claim-semantic-diff-v1",
            "noesis-evidence-freshness-policy-v1",
            "noesis-evidence-freshness-assessment-v1",
            "noesis-evidence-applicability-relation-v1",
            "noesis-evidence-freshness-impact-v1",
            "noesis-research-gap-policy-v1",
            "noesis-research-coverage-v1",
            "noesis-research-gap-v1",
            "noesis-research-gap-task-v1",
            "noesis-research-gap-report-v1",
            "noesis-source-capability-v1",
            "noesis-source-research-objective-v1",
            "noesis-source-acquisition-plan-v1",
            "noesis-source-plan-receipt-v1",
            "noesis-dataset-catalog-v1",
            "noesis-dataset-release-v1",
            "noesis-tabular-ingestion-receipt-v1",
            "noesis-dataset-slice-v1",
            "noesis-dataset-join-v1",
            "noesis-methodology-study-v1",
            "noesis-methodology-extraction-v1",
            "noesis-methodology-assessment-v1",
            "noesis-study-artifact-link-v1",
            "noesis-methodology-comparison-v1",
            "noesis-multimodal-asset-v1",
            "noesis-multimodal-extraction-v1",
            "noesis-cross-modal-evidence-v1",
            "noesis-media-authenticity-v1",
            "noesis-multimodal-search-v1",
            "noesis-citation-archive-policy-v1",
            "noesis-citation-snapshot-v1",
            "noesis-citation-verification-v1",
            "noesis-citation-health-v1",
            "noesis-citation-export-v1",
            "noesis-semantic-change-event-v1",
            "noesis-change-brief-policy-v1",
            "noesis-change-brief-v1",
            "noesis-change-brief-delivery-v1",
            "noesis-change-brief-export-v1",
            "noesis-research-recipe-v1",
            "noesis-research-recipe-preview-v1",
            "noesis-research-recipe-run-v1",
            "noesis-research-recipe-receipt-v1",
            "noesis-research-recipe-export-v1",
            "noesis-quality-policy-v1",
            "noesis-quality-assessment-v1",
            "noesis-quality-collection-v1",
            "noesis-quality-ranking-v1",
            "noesis-quality-health-v1",
            "noesis-entity-identity-decision-v1",
            "noesis-entity-merge-v1",
            "noesis-entity-split-v1",
            "noesis-entity-impact-v1",
            "noesis-entity-history-export-v1",
            "noesis-language-text-v1",
            "noesis-multilingual-alias-v1",
            "noesis-cross-language-claim-alignment-v1",
            "noesis-translation-record-v1",
            "noesis-multilingual-search-v1",
            "noesis-access-view-policy-v1",
            "noesis-access-decision-v1",
            "noesis-redacted-projection-v1",
            "noesis-share-grant-v1",
            "noesis-access-view-health-v1",
            "noesis-anomaly-watch-v1",
            "noesis-anomaly-run-v1",
            "noesis-knowledge-anomaly-v1",
            "noesis-anomaly-alert-v1",
            "noesis-anomaly-health-v1",
            "noesis-retention-policy-v1",
            "noesis-retention-checkpoint-v1",
            "noesis-archive-manifest-v1",
            "noesis-retention-gc-plan-v1",
            "noesis-retention-job-v1",
            "noesis-research-package-manifest-v1",
            "noesis-research-package-closure-v1",
            "noesis-research-package-v1",
            "noesis-research-package-verification-v1",
            "noesis-research-package-import-v1",
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
            "event-centric-knowledge-model",
            "multilingual-event-mention-clustering",
            "competing-event-accounts",
            "snapshot-bound-event-search",
            "versioned-quantitative-semantics",
            "vintage-aware-observations",
            "reproducible-quantitative-transformations",
            "series-break-comparability",
            "versioned-place-gazetteer",
            "time-bounded-wgs84-geometry",
            "ambiguity-preserving-geocoding",
            "reproducible-spatial-relations",
            "bounded-event-map-queries",
            "claim-evolution-lineage",
            "explainable-claim-successor-matching",
            "semantic-claim-state-diffs",
            "snapshot-consistent-claim-timelines",
            "versioned-evidence-freshness-policies",
            "provenance-preserving-evidence-supersession",
            "side-effect-free-freshness-simulation",
            "freshness-impact-propagation",
            "multidimensional-research-gap-records",
            "weak-support-and-citation-chain-detection",
            "deterministic-budgeted-research-planning",
            "research-gap-lifecycle-tracking",
            "credential-safe-source-capability-registry",
            "explainable-constrained-source-selection",
            "checkpointed-source-plan-execution",
            "adaptive-source-plan-fallbacks",
            "versioned-dataset-table-column-identities",
            "vintage-and-partition-aware-tabular-observations",
            "bounded-multiformat-tabular-ingestion",
            "dataset-join-discovery-and-lineage",
            "versioned-study-methodology-objects",
            "exact-locator-method-extraction",
            "reviewed-bias-and-applicability-assessments",
            "study-artifact-and-replication-graphs",
            "versioned-multimodal-assets-and-locators",
            "bounded-local-multimodal-extraction",
            "unverified-cross-modal-evidence-links",
            "media-transformation-and-authenticity-provenance",
            "policy-gated-content-addressed-citation-snapshots",
            "deterministic-citation-support-verification",
            "approved-archive-link-rot-repair",
            "dependency-complete-citation-export",
            "ranked-semantic-change-events",
            "evidence-linked-change-explanations",
            "deduplicated-windowed-brief-delivery",
            "deterministic-change-brief-export",
            "versioned-declarative-research-recipes",
            "checkpointed-resumable-recipe-runs",
            "secret-safe-per-step-policy-gates",
            "snapshot-and-tool-version-pinned-replay",
            "multidimensional-auditable-quality",
            "correlation-aware-calibrated-aggregation",
            "non-erasing-quality-aware-ranking",
            "side-effect-free-quality-policy-simulation",
            "immutable-entity-identity-decision-ledger",
            "atomic-reversible-entity-merges-and-splits",
            "selective-entity-dependency-rebuilds",
            "snapshot-aware-entity-resolution-history",
            "immutable-original-language-text",
            "reviewed-multilingual-aliases-and-transliterations",
            "ambiguity-preserving-cross-language-claim-alignment",
            "versioned-translation-provenance",
            "language-fair-multilingual-search",
            "versioned-default-deny-knowledge-views",
            "pre-ranking-access-enforcement",
            "lineage-safe-redacted-projections",
            "recipient-bound-watermarked-exports",
            "non-disclosing-access-decisions",
            "versioned-incremental-anomaly-watches",
            "bounded-replayable-anomaly-detectors",
            "uncertainty-preserving-anomaly-attribution",
            "deduplicated-recoverable-alert-delivery",
            "versioned-inherited-retention-and-legal-holds",
            "content-addressed-replayable-checkpoints",
            "verified-atomic-cold-storage-restore",
            "dependency-and-pin-safe-garbage-collection",
            "versioned-portable-research-manifests",
            "dependency-complete-policy-aware-package-closure",
            "deterministic-signed-encrypted-package-export",
            "isolated-non-executable-package-import-and-replay",
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
def create_event_record(
    namespace: str,
    event: dict[str, Any],
    event_key: str | None = None,
    lifecycle: str = "ongoing",
    granularity: str = "interval",
    generation: int = 0,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Create a stable, immutable event identity and first lifecycle revision."""
    from src.kb.events import EventKnowledgeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: EventKnowledgeStore(conn).create(
            namespace,
            event,
            principal_id=principal,
            scopes=scopes,
            event_key=event_key,
            lifecycle=lifecycle,
            granularity=granularity,
            generation=generation,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy=policy,
            provenance=provenance,
        ),
        write=True,
        required_scope="knowledge:event:write",
    )


@mcp.tool()
def revise_event_record(
    namespace: str,
    event_id: str,
    expected_revision: int,
    patch: dict[str, Any],
    reason: str,
    lifecycle: str | None = None,
) -> dict:
    """Append a correction or lifecycle revision with optimistic concurrency."""
    from src.kb.events import EventKnowledgeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: EventKnowledgeStore(conn, initialize=False).revise(
            namespace,
            event_id,
            expected_revision,
            patch,
            reason=reason,
            principal_id=principal,
            scopes=scopes,
            lifecycle=lifecycle,
        ),
        write=True,
        required_scope="knowledge:event:write",
    )


@mcp.tool()
def get_event_record(
    namespace: str,
    event_id: str,
    revision: int | None = None,
    include_history: bool = False,
) -> dict:
    """Get a current event, exact revision, or immutable history."""
    from src.kb.events import EventKnowledgeStore

    return _safe(
        lambda conn: EventKnowledgeStore(conn, initialize=False).get(
            namespace,
            event_id,
            scopes=_context()[1],
            revision=revision,
            include_history=include_history,
        ),
        required_scope="knowledge:event:read",
    )


@mcp.tool()
def get_event_record_as_of(namespace: str, event_id: str, as_of_ms: int) -> dict:
    """Read the latest event revision observed by a requested time."""
    from src.kb.events import EventKnowledgeStore

    return _safe(
        lambda conn: EventKnowledgeStore(conn, initialize=False).as_of(
            namespace, event_id, as_of_ms, scopes=_context()[1]
        ),
        required_scope="knowledge:event:read",
    )


@mcp.tool()
def ingest_event_mentions(
    namespace: str,
    document_revision_id: str,
    mentions: list[dict[str, Any]],
    language: str = "und",
    max_mentions: int = 100,
    cancel_requested: bool = False,
) -> dict:
    """Cluster a bounded multilingual mention batch using deterministic offline features."""
    from src.kb.events import EventKnowledgeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: EventKnowledgeStore(conn).ingest_mentions(
            namespace,
            document_revision_id,
            mentions,
            language=language,
            principal_id=principal,
            scopes=scopes,
            max_mentions=max_mentions,
            cancel_requested=cancel_requested,
        ),
        write=True,
        required_scope="knowledge:event:write",
    )


@mcp.tool()
def attach_event_account(
    namespace: str,
    event_id: str,
    attribute_type: str,
    value: Any,
    role: str | None = None,
    confidence: float = 1.0,
    uncertainty: float = 0.0,
    evidence: list[dict[str, Any]] | None = None,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
) -> dict:
    """Attach a sourced participant, place, time, quantity, cause, or consequence account."""
    from src.kb.events import EventKnowledgeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: EventKnowledgeStore(conn, initialize=False).attach_account(
            namespace,
            event_id,
            attribute_type,
            value,
            principal_id=principal,
            scopes=scopes,
            role=role,
            confidence=confidence,
            uncertainty=uncertainty,
            evidence=evidence or [],
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
        ),
        write=True,
        required_scope="knowledge:event:write",
    )


@mcp.tool()
def retract_event_account(namespace: str, account_id: str, reason: str) -> dict:
    """Retract a disputed event account without erasing its earlier revision."""
    from src.kb.events import EventKnowledgeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: EventKnowledgeStore(conn, initialize=False).retract_account(
            namespace,
            account_id,
            reason,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:event:review",
    )


@mcp.tool()
def list_event_accounts(
    namespace: str,
    event_id: str,
    include_retracted: bool = False,
    include_history: bool = False,
) -> dict:
    """List current competing event accounts with confidence and evidence."""
    from src.kb.events import EventKnowledgeStore

    return _safe(
        lambda conn: {
            "items": EventKnowledgeStore(conn, initialize=False).accounts(
                namespace,
                event_id,
                scopes=_context()[1],
                include_retracted=include_retracted,
                include_history=include_history,
            )
        },
        required_scope="knowledge:event:read",
    )


@mcp.tool()
def relate_events(
    namespace: str,
    from_event_id: str,
    to_event_id: str,
    relation_type: str,
    evidence: list[dict[str, Any]] | None = None,
    confidence: float = 1.0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
) -> dict:
    """Create a sourced predecessor, successor, recurrence, cause, or consequence edge."""
    from src.kb.events import EventKnowledgeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: EventKnowledgeStore(conn, initialize=False).relate(
            namespace,
            from_event_id,
            to_event_id,
            relation_type,
            principal_id=principal,
            scopes=scopes,
            evidence=evidence or [],
            confidence=confidence,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
        ),
        write=True,
        required_scope="knowledge:event:write",
    )


@mcp.tool()
def search_event_records(
    namespace: str,
    event_types: list[str] | None = None,
    lifecycles: list[str] | None = None,
    query: str | None = None,
    snapshot_generation: int | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    """Search event records with a snapshot-bound opaque pagination cursor."""
    from src.kb.events import EventKnowledgeStore

    return _safe(
        lambda conn: EventKnowledgeStore(conn, initialize=False).search(
            namespace,
            scopes=_context()[1],
            event_types=event_types or [],
            lifecycles=lifecycles or [],
            query=query,
            snapshot_generation=snapshot_generation,
            limit=limit,
            cursor=cursor,
        ),
        required_scope="knowledge:event:read",
    )


@mcp.tool()
def event_timeline(
    namespace: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 100,
) -> dict:
    """Return a bounded chronological event timeline."""
    from src.kb.events import EventKnowledgeStore

    return _safe(
        lambda conn: {
            "items": EventKnowledgeStore(conn, initialize=False).timeline(
                namespace,
                scopes=_context()[1],
                start_ms=start_ms,
                end_ms=end_ms,
                limit=limit,
            )
        },
        required_scope="knowledge:event:read",
    )


@mcp.tool()
def event_neighborhood(event_id: str, max_depth: int = 2) -> dict:
    """Traverse a bounded cross-domain event-relation neighborhood."""
    from src.kb.events import EventKnowledgeStore

    return _safe(
        lambda conn: EventKnowledgeStore(conn, initialize=False).neighborhood(
            event_id, scopes=_context()[1], max_depth=max_depth
        ),
        required_scope="knowledge:event:read",
    )


@mcp.tool()
def diff_event_revisions(
    namespace: str, event_id: str, from_revision: int, to_revision: int
) -> dict:
    """Return a stable semantic diff between two event revisions."""
    from src.kb.events import EventKnowledgeStore

    return _safe(
        lambda conn: EventKnowledgeStore(conn, initialize=False).diff(
            namespace,
            event_id,
            from_revision,
            to_revision,
            scopes=_context()[1],
        ),
        required_scope="knowledge:event:read",
    )


@mcp.tool()
def replay_event_record(namespace: str, event_id: str) -> dict:
    """Replay and verify an event's immutable predecessor chain."""
    from src.kb.events import EventKnowledgeStore

    return _safe(
        lambda conn: EventKnowledgeStore(conn, initialize=False).replay(
            namespace, event_id, scopes=_context()[1]
        ),
        required_scope="knowledge:event:read",
    )


@mcp.tool()
def register_quantitative_unit(
    namespace: str,
    symbol: str,
    dimension: dict[str, int],
    factor: str = "1",
    offset: str = "0",
    aliases: list[str] | None = None,
    currency_code: str | None = None,
    successor_unit_id: str | None = None,
    redenomination_factor: str | None = None,
    semantic_version: str = "1.0.0",
) -> dict:
    """Register an immutable unit, compound dimension, or currency version."""
    from src.kb.quantitative import QuantitativeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: QuantitativeStore(conn).register_unit(
            namespace,
            symbol,
            dimension,
            factor=factor,
            offset=offset,
            aliases=aliases or [],
            currency_code=currency_code,
            successor_unit_id=successor_unit_id,
            redenomination_factor=redenomination_factor,
            semantic_version=semantic_version,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:quantitative:write",
    )


@mcp.tool()
def register_quantitative_metric(
    namespace: str,
    canonical_name: str,
    definition: str,
    unit: str,
    frequency: str = "irregular",
    population: dict[str, Any] | None = None,
    synonyms: list[str] | None = None,
    mappings: dict[str, str] | None = None,
    formula: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict:
    """Register a versioned metric with aliases and source-native mappings."""
    from src.kb.quantitative import QuantitativeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: QuantitativeStore(conn).register_metric(
            namespace,
            canonical_name,
            definition,
            unit,
            frequency=frequency,
            population=population,
            synonyms=synonyms or [],
            mappings=mappings,
            formula=formula,
            idempotency_key=idempotency_key,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy=policy,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:quantitative:write",
    )


@mcp.tool()
def revise_quantitative_metric(
    namespace: str,
    metric_id: str,
    expected_revision: int,
    patch: dict[str, Any],
) -> dict:
    """Append an immutable metric schema or methodology revision."""
    from src.kb.quantitative import QuantitativeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: QuantitativeStore(conn, initialize=False).revise_metric(
            namespace,
            metric_id,
            expected_revision,
            patch,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:quantitative:write",
    )


@mcp.tool()
def record_quantitative_observation(
    namespace: str,
    metric_id: str,
    period: str,
    value: Any | None,
    provider: str,
    provider_series_id: str,
    vintage_id: str,
    release_at_ms: int,
    retrieved_at_ms: int,
    unit: str | None = None,
    currency_code: str | None = None,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    adjustment: str = "unknown",
    preliminary: bool = False,
    revision_of: str | None = None,
    provenance: dict[str, Any] | None = None,
    generation: int = 0,
) -> dict:
    """Record a provenance-rich observation without replacing earlier vintages."""
    from src.kb.quantitative import QuantitativeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: QuantitativeStore(conn, initialize=False).observe(
            namespace,
            metric_id,
            period,
            value,
            provider=provider,
            provider_series_id=provider_series_id,
            vintage_id=vintage_id,
            release_at_ms=release_at_ms,
            retrieved_at_ms=retrieved_at_ms,
            unit=unit,
            currency_code=currency_code,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            adjustment=adjustment,
            preliminary=preliminary,
            revision_of=revision_of,
            provenance=provenance,
            generation=generation,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:quantitative:write",
    )


@mcp.tool()
def record_quantitative_series_break(
    namespace: str,
    metric_id: str,
    break_type: str,
    boundary_ms: int,
    before: dict[str, Any],
    after: dict[str, Any],
    evidence: list[dict[str, Any]],
    confidence: float,
) -> dict:
    """Record a sourced definition, method, geography, rebase, basket, or provider break."""
    from src.kb.quantitative import QuantitativeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: QuantitativeStore(conn, initialize=False).add_break(
            namespace,
            metric_id,
            break_type,
            boundary_ms,
            before=before,
            after=after,
            evidence=evidence,
            confidence=confidence,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:quantitative:write",
    )


@mcp.tool()
def discover_quantitative_metrics(
    namespace: str, query: str = "", limit: int = 50
) -> dict:
    """Discover metrics by canonical name, synonym, or provider mapping."""
    from src.kb.quantitative import QuantitativeStore

    return _safe(
        lambda conn: {
            "items": QuantitativeStore(conn, initialize=False).discover(
                namespace, scopes=_context()[1], query=query, limit=limit
            )
        },
        required_scope="knowledge:quantitative:read",
    )


@mcp.tool()
def get_quantitative_metric(
    namespace: str,
    metric_id: str,
    revision: int | None = None,
    include_history: bool = False,
) -> dict:
    """Read the current, exact, or complete immutable history of a metric."""
    from src.kb.quantitative import QuantitativeStore

    return _safe(
        lambda conn: QuantitativeStore(conn, initialize=False).metric(
            namespace,
            metric_id,
            scopes=_context()[1],
            revision=revision,
            include_history=include_history,
        ),
        required_scope="knowledge:quantitative:read",
    )


@mcp.tool()
def read_quantitative_series(
    namespace: str,
    metric_id: str,
    as_of_ms: int | None = None,
    provider: str | None = None,
    include_vintages: bool = False,
    limit: int = 1000,
) -> dict:
    """Read coherent latest vintages, or all vintages, as known at a requested time."""
    from src.kb.quantitative import QuantitativeStore

    return _safe(
        lambda conn: {
            "items": QuantitativeStore(conn, initialize=False).series(
                namespace,
                metric_id,
                scopes=_context()[1],
                as_of_ms=as_of_ms,
                provider=provider,
                include_vintages=include_vintages,
                limit=limit,
            ),
            "as_of_ms": as_of_ms,
            "include_vintages": include_vintages,
        },
        required_scope="knowledge:quantitative:read",
    )


@mcp.tool()
def assess_quantitative_comparability(
    namespace: str, left_observation_id: str, right_observation_id: str
) -> dict:
    """Explain whether observations are comparable across adjustments and series breaks."""
    from src.kb.quantitative import QuantitativeStore

    return _safe(
        lambda conn: QuantitativeStore(conn, initialize=False).comparability(
            namespace,
            left_observation_id,
            right_observation_id,
            scopes=_context()[1],
        ),
        required_scope="knowledge:quantitative:read",
    )


@mcp.tool()
def convert_quantitative_value(
    namespace: str,
    value: Any,
    from_unit: str,
    to_unit: str,
    precision: int = 6,
    rate: dict[str, Any] | None = None,
) -> dict:
    """Convert a value using exact units, explicit FX evidence, and a durable receipt."""
    from src.kb.quantitative import QuantitativeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: QuantitativeStore(conn, initialize=False).convert(
            namespace,
            value,
            from_unit,
            to_unit,
            precision=precision,
            rate=rate,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:quantitative:calculate",
    )


@mcp.tool()
def evaluate_quantitative_formula(
    namespace: str,
    metric_id: str,
    inputs: dict[str, dict[str, Any]],
    precision: int = 6,
) -> dict:
    """Evaluate a safe versioned metric formula with exact input lineage."""
    from src.kb.quantitative import QuantitativeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: QuantitativeStore(conn, initialize=False).evaluate_formula(
            namespace,
            metric_id,
            inputs,
            precision=precision,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:quantitative:calculate",
    )


@mcp.tool()
def transform_quantitative_frequency(
    namespace: str,
    values: list[dict[str, Any]],
    from_frequency: str,
    to_frequency: str,
    aggregation: str,
    precision: int = 6,
) -> dict:
    """Aggregate a complete input window to another frequency with a receipt."""
    from src.kb.quantitative import QuantitativeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: QuantitativeStore(conn, initialize=False).transform_frequency(
            namespace,
            values,
            from_frequency=from_frequency,
            to_frequency=to_frequency,
            aggregation=aggregation,
            precision=precision,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:quantitative:calculate",
    )


@mcp.tool()
def adjust_quantitative_inflation(
    namespace: str,
    value: Any,
    observed_index: dict[str, Any],
    target_index: dict[str, Any],
    precision: int = 6,
) -> dict:
    """Adjust a value between explicit price-index observations with lineage."""
    from src.kb.quantitative import QuantitativeStore

    principal, scopes = _context()
    return _safe(
        lambda conn: QuantitativeStore(conn, initialize=False).adjust_inflation(
            namespace,
            value,
            observed_index,
            target_index,
            precision=precision,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:quantitative:calculate",
    )


@mcp.tool()
def replay_quantitative_calculation(namespace: str, calculation_id: str) -> dict:
    """Recompute a stored calculation hash to verify deterministic replay."""
    from src.kb.quantitative import QuantitativeStore

    return _safe(
        lambda conn: QuantitativeStore(conn, initialize=False).replay_calculation(
            namespace, calculation_id, scopes=_context()[1]
        ),
        required_scope="knowledge:quantitative:read",
    )


@mcp.tool()
def register_geospatial_place(
    namespace: str,
    canonical_name: str,
    place_type: str,
    names: list[dict[str, Any]],
    source_ids: dict[str, str],
    parent_ids: list[str] | None = None,
    place_key: str | None = None,
    geometry: dict[str, Any] | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Register a stable versioned place identity and optional bootstrap geometry."""
    from src.kb.geospatial import GeospatialStore

    principal, scopes = _context()
    return _safe(
        lambda conn: GeospatialStore(conn).register_place(
            namespace,
            canonical_name,
            place_type,
            names=names,
            source_ids=source_ids,
            parent_ids=parent_ids or [],
            place_key=place_key,
            geometry=geometry,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            provenance=provenance,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:geospatial:write",
    )


@mcp.tool()
def revise_geospatial_place(
    namespace: str,
    place_id: str,
    expected_revision: int,
    patch: dict[str, Any],
    reason: str,
) -> dict:
    """Append an immutable correction, name history, or hierarchy revision."""
    from src.kb.geospatial import GeospatialStore

    principal, scopes = _context()
    return _safe(
        lambda conn: GeospatialStore(conn, initialize=False).revise_place(
            namespace,
            place_id,
            expected_revision,
            patch,
            reason=reason,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:geospatial:write",
    )


@mcp.tool()
def get_geospatial_place(
    namespace: str,
    place_id: str,
    revision: int | None = None,
    include_history: bool = False,
) -> dict:
    """Read the current, exact, or complete history of a place."""
    from src.kb.geospatial import GeospatialStore

    return _safe(
        lambda conn: GeospatialStore(conn, initialize=False).place(
            namespace,
            place_id,
            scopes=_context()[1],
            revision=revision,
            include_history=include_history,
        ),
        required_scope="knowledge:geospatial:read",
    )


@mcp.tool()
def store_geospatial_geometry(
    namespace: str,
    geometry: dict[str, Any],
    place_id: str | None,
    source: dict[str, Any],
    evidence: list[dict[str, Any]],
    crs: str = "EPSG:4326",
    precision_m: float = 0,
    simplified_from: str | None = None,
    disputed: bool = False,
    admin_hierarchy: list[str] | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
) -> dict:
    """Store an immutable point, line, or polygon with time and source context."""
    from src.kb.geospatial import GeospatialStore

    principal, scopes = _context()
    return _safe(
        lambda conn: GeospatialStore(conn, initialize=False).store_geometry(
            namespace,
            geometry,
            place_id=place_id,
            crs=crs,
            precision_m=precision_m,
            simplified_from=simplified_from,
            disputed=disputed,
            admin_hierarchy=admin_hierarchy or [],
            source=source,
            evidence=evidence,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:geospatial:write",
    )


@mcp.tool()
def list_geospatial_geometries(
    namespace: str,
    place_id: str,
    as_of_ms: int | None = None,
    include_disputed: bool = True,
) -> dict:
    """List a place's geometries valid at a requested time."""
    from src.kb.geospatial import GeospatialStore

    return _safe(
        lambda conn: {
            "items": GeospatialStore(conn, initialize=False).geometries(
                namespace,
                place_id,
                scopes=_context()[1],
                as_of_ms=as_of_ms,
                include_disputed=include_disputed,
            )
        },
        required_scope="knowledge:geospatial:read",
    )


@mcp.tool()
def simplify_geospatial_geometry(
    namespace: str, geometry_id: str, tolerance_m: float
) -> dict:
    """Create a source-linked simplified geometry at an explicit tolerance."""
    from src.kb.geospatial import GeospatialStore

    principal, scopes = _context()
    return _safe(
        lambda conn: GeospatialStore(conn, initialize=False).simplify(
            namespace,
            geometry_id,
            tolerance_m,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:geospatial:write",
    )


@mcp.tool()
def resolve_geospatial_candidates(
    namespace: str,
    mention: str,
    context: dict[str, Any] | None = None,
    coordinate_hint: list[float] | None = None,
    as_of_ms: int | None = None,
    limit: int = 10,
) -> dict:
    """Resolve text against offline place candidates without hiding ambiguity."""
    from src.kb.geospatial import GeospatialStore

    return _safe(
        lambda conn: GeospatialStore(conn, initialize=False).resolve(
            namespace,
            mention,
            context=context,
            coordinate_hint=coordinate_hint,
            as_of_ms=as_of_ms,
            limit=limit,
            scopes=_context()[1],
        ),
        required_scope="knowledge:geospatial:read",
    )


@mcp.tool()
def record_geospatial_resolution(
    namespace: str,
    mention: str,
    context: dict[str, Any] | None = None,
    coordinate_hint: list[float] | None = None,
    as_of_ms: int | None = None,
    limit: int = 10,
) -> dict:
    """Persist an ambiguity-preserving offline geocoding result for review."""
    from src.kb.geospatial import GeospatialStore

    principal, scopes = _context()

    def operation(conn):
        store = GeospatialStore(conn, initialize=False)
        result = store.resolve(
            namespace,
            mention,
            context=context,
            coordinate_hint=coordinate_hint,
            as_of_ms=as_of_ms,
            limit=limit,
            scopes={"knowledge:geospatial:read"},
        )
        return store.save_resolution(result, principal_id=principal, scopes=scopes)

    return _safe(
        operation,
        write=True,
        required_scope="knowledge:geospatial:write",
    )


@mcp.tool()
def review_geospatial_resolution(
    namespace: str,
    resolution_id: str,
    decision: str,
    reason: str,
    selected_place_id: str | None = None,
) -> dict:
    """Append a reversible accept, reject, or defer review decision."""
    from src.kb.geospatial import GeospatialStore

    principal, scopes = _context()
    return _safe(
        lambda conn: GeospatialStore(conn, initialize=False).review(
            namespace,
            resolution_id,
            decision,
            selected_place_id=selected_place_id,
            reason=reason,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:geospatial:review",
    )


@mcp.tool()
def calculate_spatial_relation(
    namespace: str,
    operation: str,
    left_geometry_id: str,
    right: Any = None,
    tolerance_m: float = 0,
) -> dict:
    """Calculate containment, proximity, intersection, or route length with a receipt."""
    from src.kb.geospatial import GeospatialStore

    principal, scopes = _context()
    return _safe(
        lambda conn: GeospatialStore(conn, initialize=False).relation(
            namespace,
            operation,
            left_geometry_id,
            right,
            tolerance_m=tolerance_m,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:geospatial:calculate",
    )


@mcp.tool()
def replay_spatial_relation(namespace: str, receipt_id: str) -> dict:
    """Verify a spatial calculation receipt from its pinned inputs and algorithm."""
    from src.kb.geospatial import GeospatialStore

    return _safe(
        lambda conn: GeospatialStore(conn, initialize=False).replay(
            namespace, receipt_id, scopes=_context()[1]
        ),
        required_scope="knowledge:geospatial:read",
    )


@mcp.tool()
def search_geospatial_knowledge(
    namespace: str,
    bbox: list[float] | None = None,
    center: list[float] | None = None,
    radius_m: float | None = None,
    contains_point: list[float] | None = None,
    as_of_ms: int | None = None,
    include_disputed: bool = True,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    """Search bounded boxes, radii, and containment with cursor-bound filters."""
    from src.kb.geospatial import GeospatialStore

    return _safe(
        lambda conn: GeospatialStore(conn, initialize=False).search(
            namespace,
            bbox=bbox,
            center=center,
            radius_m=radius_m,
            contains_point=contains_point,
            as_of_ms=as_of_ms,
            include_disputed=include_disputed,
            limit=limit,
            cursor=cursor,
            scopes=_context()[1],
        ),
        required_scope="knowledge:geospatial:read",
    )


@mcp.tool()
def query_geospatial_event_map(
    namespace: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 100,
) -> dict:
    """Return bounded current event locations for political and OSINT maps."""
    from src.kb.geospatial import GeospatialStore

    return _safe(
        lambda conn: GeospatialStore(conn, initialize=False).event_map(
            namespace,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=limit,
            scopes=_context()[1],
        ),
        required_scope="knowledge:geospatial:read",
    )


@mcp.tool()
def capture_claim_timeline_state(
    namespace: str,
    claim_id: str,
    source_id: str,
    source_revision_id: str,
    evidence: list[dict[str, Any]],
    wording: str | None = None,
    stance: str = "unknown",
    certainty: float = 0.5,
    epistemic_status: str = "unassessed",
    attribution: dict[str, Any] | None = None,
    quantities: list[dict[str, Any]] | None = None,
    scope: dict[str, Any] | None = None,
    interpretations: list[dict[str, Any]] | None = None,
    source_retracted: bool = False,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Capture an immutable semantic state for an existing canonical claim."""
    from src.kb.claim_timelines import ClaimTimelineStore

    principal, scopes = _context()
    return _safe(
        lambda conn: ClaimTimelineStore(conn).capture_state(
            namespace,
            claim_id,
            wording=wording,
            stance=stance,
            certainty=certainty,
            epistemic_status=epistemic_status,
            attribution=attribution,
            quantities=quantities or [],
            scope=scope,
            interpretations=interpretations or [],
            evidence=evidence,
            source_id=source_id,
            source_revision_id=source_revision_id,
            source_retracted=source_retracted,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            provenance=provenance,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:claim-timeline:write",
    )


@mcp.tool()
def link_claim_evolution(
    namespace: str,
    predecessor_claim_id: str,
    successor_claim_id: str,
    relation: str,
    confidence: float,
    evidence: list[dict[str, Any]],
    explanation: dict[str, Any],
    method: dict[str, Any],
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Link existing claims with a sourced successor, refinement, reversal, withdrawal, or branch."""
    from src.kb.claim_timelines import ClaimTimelineStore

    principal, scopes = _context()
    return _safe(
        lambda conn: ClaimTimelineStore(conn, initialize=False).link(
            namespace,
            predecessor_claim_id,
            successor_claim_id,
            relation,
            confidence=confidence,
            evidence=evidence,
            explanation=explanation,
            method=method,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            provenance=provenance,
            principal_id=principal,
            scopes=scopes,
        ),
        write=True,
        required_scope="knowledge:claim-timeline:write",
    )


@mcp.tool()
def get_claim_timeline_state(
    namespace: str,
    claim_id: str,
    as_of_ms: int | None = None,
    generation: int | None = None,
) -> dict:
    """Read the latest claim state at an observation time and generation."""
    from src.kb.claim_timelines import ClaimTimelineStore

    return _safe(
        lambda conn: ClaimTimelineStore(conn, initialize=False).latest(
            namespace,
            claim_id,
            as_of_ms=as_of_ms,
            generation=generation,
            scopes=_context()[1],
        ),
        required_scope="knowledge:claim-timeline:read",
    )


@mcp.tool()
def detect_claim_successors(
    namespace: str,
    claim_id: str,
    candidate_claim_ids: list[str] | None = None,
    threshold: float = 0.45,
    limit: int = 20,
    embedding_scores: dict[str, float] | None = None,
    embedding_pin: dict[str, Any] | None = None,
    cancel_requested: bool = False,
) -> dict:
    """Rank related claim versions with deterministic signals and an optional pinned embedding."""
    from src.kb.claim_timelines import ClaimTimelineStore

    return _safe(
        lambda conn: ClaimTimelineStore(conn, initialize=False).match_successors(
            namespace,
            claim_id,
            candidate_claim_ids=candidate_claim_ids or [],
            threshold=threshold,
            limit=limit,
            embedding_scores=embedding_scores,
            embedding_pin=embedding_pin,
            persist=False,
            cancel_requested=cancel_requested,
            principal_id=_context()[0],
            scopes=_context()[1],
        ),
        required_scope="knowledge:claim-timeline:read",
    )


@mcp.tool()
def diff_claim_timeline_states(
    namespace: str,
    left_claim_id: str,
    right_claim_id: str,
    left_revision: int | None = None,
    right_revision: int | None = None,
) -> dict:
    """Explain wording, stance, certainty, attribution, scope, and quantitative changes."""
    from src.kb.claim_timelines import ClaimTimelineStore

    return _safe(
        lambda conn: ClaimTimelineStore(conn, initialize=False).diff(
            namespace,
            left_claim_id,
            right_claim_id,
            left_revision=left_revision,
            right_revision=right_revision,
            scopes=_context()[1],
        ),
        required_scope="knowledge:claim-timeline:read",
    )


@mcp.tool()
def get_claim_evolution_timeline(
    namespace: str,
    claim_id: str,
    as_of_ms: int | None = None,
    generation: int | None = None,
    max_depth: int = 6,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    """Build a snapshot-consistent, branching, paginated claim timeline."""
    from src.kb.claim_timelines import ClaimTimelineStore

    return _safe(
        lambda conn: ClaimTimelineStore(conn, initialize=False).timeline(
            namespace,
            claim_id,
            as_of_ms=as_of_ms,
            generation=generation,
            max_depth=max_depth,
            limit=limit,
            cursor=cursor,
            scopes=_context()[1],
        ),
        required_scope="knowledge:claim-timeline:read",
    )


@mcp.tool()
def compare_claim_sources(
    namespace: str, source_ids: list[str], limit: int = 50
) -> dict:
    """Compare current cited claim states across selected sources."""
    from src.kb.claim_timelines import ClaimTimelineStore

    return _safe(
        lambda conn: ClaimTimelineStore(conn, initialize=False).compare_sources(
            namespace, source_ids, limit=limit, scopes=_context()[1]
        ),
        required_scope="knowledge:claim-timeline:read",
    )


@mcp.tool()
def replay_claim_evolution(namespace: str, claim_id: str) -> dict:
    """Replay a claim evolution component and verify its citation closure."""
    from src.kb.claim_timelines import ClaimTimelineStore

    return _safe(
        lambda conn: ClaimTimelineStore(conn, initialize=False).replay(
            namespace, claim_id, scopes=_context()[1]
        ),
        required_scope="knowledge:claim-timeline:read",
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


@mcp.tool()
def register_evidence_freshness_policy(
    namespace: str,
    domain: str,
    source_type: str,
    object_type: str,
    semantic_version: str,
    rules: dict[str, Any],
    supersedes_policy_id: str | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy_context: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Register an immutable, versioned freshness policy and optionally activate an upgrade."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn).register_policy(
            namespace,
            domain,
            source_type,
            object_type,
            semantic_version,
            rules,
            supersedes_policy_id=supersedes_policy_id,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy_context=policy_context,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:freshness:write"},
        ),
        write=True,
        required_scope="knowledge:freshness:write",
    )


@mcp.tool()
def get_evidence_freshness_policy(namespace: str, policy_id: str) -> dict:
    """Read a freshness policy, including supersession and provenance metadata."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn, initialize=False).policy(
            policy_id, namespace=namespace, scopes={"knowledge:freshness:read"}
        ),
        required_scope="knowledge:freshness:read",
    )


@mcp.tool()
def annotate_evidence_freshness(
    namespace: str,
    evidence_id: str,
    domain: str,
    source_type: str,
    object_type: str,
    retrieved_at_ms: int,
    published_at_ms: int | None = None,
    observed_at_ms: int | None = None,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    event_closed_at_ms: int | None = None,
    methodology_revision: str | None = None,
    source_health: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    generation: int = 0,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict:
    """Attach immutable lifecycle metadata to an existing evidence identity."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn).annotate(
            namespace,
            evidence_id,
            domain,
            source_type,
            object_type,
            retrieved_at_ms=retrieved_at_ms,
            published_at_ms=published_at_ms,
            observed_at_ms=observed_at_ms,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            event_closed_at_ms=event_closed_at_ms,
            methodology_revision=methodology_revision,
            source_health=source_health,
            provenance=provenance,
            generation=generation,
            producer=producer,
            policy=policy,
            principal_id=_context()[0],
            scopes={"knowledge:freshness:write"},
        ),
        write=True,
        required_scope="knowledge:freshness:write",
    )


@mcp.tool()
def relate_evidence_applicability(
    namespace: str,
    earlier_evidence_id: str,
    later_evidence_id: str,
    relation: str,
    applicability: dict[str, Any],
    confidence: float,
    evidence: list[dict[str, Any]],
    provenance: dict[str, Any],
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
) -> dict:
    """Record exactly why later evidence supersedes, narrows, or invalidates earlier evidence."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn).relate(
            namespace,
            earlier_evidence_id,
            later_evidence_id,
            relation,
            applicability=applicability,
            confidence=confidence,
            evidence=evidence,
            provenance=provenance,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            principal_id=_context()[0],
            scopes={"knowledge:freshness:write"},
        ),
        write=True,
        required_scope="knowledge:freshness:write",
    )


@mcp.tool()
def review_evidence_freshness_override(
    namespace: str,
    evidence_id: str,
    state: str,
    reason: str,
    evidence: list[dict[str, Any]],
    valid_until_ms: int | None = None,
) -> dict:
    """Apply an auditable, optionally expiring human freshness decision."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn).override(
            namespace,
            evidence_id,
            state,
            valid_until_ms=valid_until_ms,
            reason=reason,
            evidence=evidence,
            principal_id=_context()[0],
            scopes={"knowledge:freshness:review"},
        ),
        write=True,
        required_scope="knowledge:freshness:review",
    )


@mcp.tool()
def assess_evidence_freshness(
    namespace: str,
    evidence_id: str,
    at_ms: int,
    policy_version: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict:
    """Persist a deterministic freshness assessment and full explanation trace."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn).assess(
            namespace,
            evidence_id,
            at_ms=at_ms,
            policy_version=policy_version,
            context=context,
            principal_id=_context()[0],
            scopes={"knowledge:freshness:write"},
        ),
        write=True,
        required_scope="knowledge:freshness:write",
    )


@mcp.tool()
def get_evidence_freshness_assessment(namespace: str, assessment_id: str) -> dict:
    """Read a stored freshness decision and the reasons behind it."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn, initialize=False).assessment(
            namespace, assessment_id, scopes={"knowledge:freshness:read"}
        ),
        required_scope="knowledge:freshness:read",
    )


@mcp.tool()
def list_expiring_evidence(
    namespace: str,
    at_ms: int,
    horizon_ms: int,
    limit: int = 100,
    cancel_requested: bool = False,
) -> dict:
    """Scan a bounded evidence set for items that expire or become stale soon."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn, initialize=False).expiring(
            namespace,
            at_ms=at_ms,
            horizon_ms=horizon_ms,
            limit=limit,
            cancel_requested=cancel_requested,
            scopes={"knowledge:freshness:read"},
        ),
        required_scope="knowledge:freshness:read",
    )


@mcp.tool()
def simulate_evidence_freshness_policy(
    namespace: str,
    evidence_ids: list[str],
    at_ms: int,
    policy_version: str | None = None,
    policy_override: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    limit: int = 100,
    cancel_requested: bool = False,
) -> dict:
    """Simulate freshness policy changes without creating assessments or audit rows."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn, initialize=False).simulate(
            namespace,
            evidence_ids,
            at_ms=at_ms,
            policy_version=policy_version,
            policy_override=policy_override,
            context=context,
            limit=limit,
            cancel_requested=cancel_requested,
            scopes={"knowledge:freshness:read"},
        ),
        required_scope="knowledge:freshness:read",
    )


@mcp.tool()
def compare_evidence_freshness_policies(
    namespace: str,
    evidence_ids: list[str],
    at_ms: int,
    old_version: str,
    new_version: str,
    context: dict[str, Any] | None = None,
    limit: int = 100,
) -> dict:
    """Compare immutable policy versions over a bounded evidence set without side effects."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn, initialize=False).compare_policies(
            namespace,
            evidence_ids,
            at_ms=at_ms,
            old_version=old_version,
            new_version=new_version,
            context=context,
            limit=limit,
            scopes={"knowledge:freshness:read"},
        ),
        required_scope="knowledge:freshness:read",
    )


@mcp.tool()
def register_evidence_freshness_dependency(
    namespace: str,
    evidence_id: str,
    consumer_kind: str,
    consumer_id: str,
    detail: dict[str, Any] | None = None,
) -> dict:
    """Link evidence to a claim, answer, brief, watch, search result, or assessment."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn).dependency(
            namespace,
            evidence_id,
            consumer_kind,
            consumer_id,
            detail or {},
            principal_id=_context()[0],
            scopes={"knowledge:freshness:write"},
        ),
        write=True,
        required_scope="knowledge:freshness:write",
    )


@mcp.tool()
def propagate_evidence_freshness(
    namespace: str,
    at_ms: int,
    limit: int = 100,
    cancel_requested: bool = False,
) -> dict:
    """Propagate freshness into dependent knowledge products with alert deduplication."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn).propagate(
            namespace,
            at_ms=at_ms,
            principal_id=_context()[0],
            scopes={"knowledge:freshness:write"},
            limit=limit,
            cancel_requested=cancel_requested,
        ),
        write=True,
        required_scope="knowledge:freshness:write",
    )


@mcp.tool()
def replay_evidence_freshness_assessment(namespace: str, assessment_id: str) -> dict:
    """Verify a stored freshness calculation against its canonical hash."""
    from src.kb.freshness import EvidenceFreshnessStore

    return _safe(
        lambda conn: EvidenceFreshnessStore(conn, initialize=False).replay(
            namespace, assessment_id, scopes={"knowledge:freshness:read"}
        ),
        required_scope="knowledge:freshness:read",
    )


@mcp.tool()
def register_research_gap_policy(
    namespace: str,
    semantic_version: str,
    thresholds: dict[str, Any],
    weights: dict[str, Any],
    supersedes_policy_id: str | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy_context: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Register immutable support thresholds and research-task ranking weights."""
    from src.kb.research_gaps import ResearchGapStore

    return _safe(
        lambda conn: ResearchGapStore(conn).register_policy(
            namespace,
            semantic_version,
            thresholds,
            weights,
            supersedes_policy_id=supersedes_policy_id,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy_context=policy_context,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:gaps:write"},
        ),
        write=True,
        required_scope="knowledge:gaps:write",
    )


@mcp.tool()
def record_research_coverage(
    namespace: str,
    object_kind: str,
    object_id: str,
    dimension: dict[str, Any],
    coverage_known: bool,
    supports: list[dict[str, Any]],
    signals: dict[str, Any] | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Record a versioned coverage observation against an existing knowledge identity."""
    from src.kb.research_gaps import ResearchGapStore

    return _safe(
        lambda conn: ResearchGapStore(conn).observe(
            namespace,
            object_kind,
            object_id,
            dimension,
            coverage_known=coverage_known,
            supports=supports,
            signals=signals or {},
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy=policy,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:gaps:write"},
        ),
        write=True,
        required_scope="knowledge:gaps:write",
    )


@mcp.tool()
def discover_research_gaps(
    namespace: str,
    policy_version: str | None = None,
    object_kind: str | None = None,
    limit: int = 100,
    cancel_requested: bool = False,
) -> dict:
    """Detect bounded weak-support, contradiction, citation-chain, and coverage gaps."""
    from src.kb.research_gaps import ResearchGapStore

    return _safe(
        lambda conn: ResearchGapStore(conn).discover(
            namespace,
            policy_version=policy_version,
            object_kind=object_kind,
            limit=limit,
            cancel_requested=cancel_requested,
            principal_id=_context()[0],
            scopes={"knowledge:gaps:write"},
        ),
        write=True,
        required_scope="knowledge:gaps:write",
    )


@mcp.tool()
def get_research_gap(namespace: str, gap_id: str) -> dict:
    """Read the current immutable revision of one research gap."""
    from src.kb.research_gaps import ResearchGapStore

    return _safe(
        lambda conn: ResearchGapStore(conn, initialize=False).get(
            namespace, gap_id, scopes={"knowledge:gaps:read"}
        ),
        required_scope="knowledge:gaps:read",
    )


@mcp.tool()
def explain_research_gap(namespace: str, gap_id: str) -> dict:
    """Drill into thresholds, evidence identities, provenance, and ranking signals."""
    from src.kb.research_gaps import ResearchGapStore

    def operation(conn):
        gap = ResearchGapStore(conn, initialize=False).get(
            namespace, gap_id, scopes={"knowledge:gaps:read"}
        )
        if gap is None:
            return None
        return {
            "gap_id": gap_id,
            "gap_type": gap["gap_type"],
            "status": gap["status"],
            "dimension": gap["dimension"],
            "detail": gap["detail"],
            "explanation": gap["explanation"],
            "provenance": gap["provenance"],
            "content_hash": gap["content_hash"],
        }

    return _safe(operation, required_scope="knowledge:gaps:read")


@mcp.tool()
def list_research_gaps(
    namespace: str,
    status: str | None = None,
    gap_type: str | None = None,
    object_kind: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict:
    """Page deterministically through research gaps within one namespace."""
    from src.kb.research_gaps import ResearchGapStore

    return _safe(
        lambda conn: ResearchGapStore(conn, initialize=False).list(
            namespace,
            status=status,
            gap_type=gap_type,
            object_kind=object_kind,
            limit=limit,
            cursor=cursor,
            scopes={"knowledge:gaps:read"},
        ),
        required_scope="knowledge:gaps:read",
    )


@mcp.tool()
def update_research_gap_status(
    namespace: str,
    gap_id: str,
    status: str,
    reason: str,
    evidence: list[dict[str, Any]],
) -> dict:
    """Review, progress, resolve, or dismiss a gap with explicit evidence."""
    from src.kb.research_gaps import ResearchGapStore

    return _safe(
        lambda conn: ResearchGapStore(conn).set_status(
            namespace,
            gap_id,
            status,
            reason=reason,
            evidence=evidence,
            principal_id=_context()[0],
            scopes={"knowledge:gaps:review"},
        ),
        write=True,
        required_scope="knowledge:gaps:review",
    )


@mcp.tool()
def prioritize_research_gaps(
    namespace: str,
    budget: float,
    max_tasks: int = 25,
    blocked_source_classes: list[str] | None = None,
    policy_version: str | None = None,
) -> dict:
    """Rank open gaps and persist executable suggestions within a hard budget."""
    from src.kb.research_gaps import ResearchGapStore

    return _safe(
        lambda conn: ResearchGapStore(conn).prioritize(
            namespace,
            budget=budget,
            max_tasks=max_tasks,
            blocked_source_classes=blocked_source_classes or [],
            policy_version=policy_version,
            principal_id=_context()[0],
            scopes={"knowledge:gaps:write"},
        ),
        write=True,
        required_scope="knowledge:gaps:write",
    )


@mcp.tool()
def list_research_gap_tasks(
    namespace: str,
    status: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict:
    """List generated research tasks and their current execution status."""
    from src.kb.research_gaps import ResearchGapStore

    return _safe(
        lambda conn: ResearchGapStore(conn, initialize=False).tasks(
            namespace,
            status=status,
            limit=limit,
            cursor=cursor,
            scopes={"knowledge:gaps:read"},
        ),
        required_scope="knowledge:gaps:read",
    )


@mcp.tool()
def compare_research_gap_coverage(
    namespace: str, before_observed_ms: int, after_observed_ms: int
) -> dict:
    """Compare gap counts and coverage score before and after research work."""
    from src.kb.research_gaps import ResearchGapStore

    return _safe(
        lambda conn: ResearchGapStore(conn, initialize=False).compare_coverage(
            namespace,
            before_observed_ms,
            after_observed_ms,
            scopes={"knowledge:gaps:read"},
        ),
        required_scope="knowledge:gaps:read",
    )


@mcp.tool()
def replay_research_gap(namespace: str, gap_id: str) -> dict:
    """Verify the current gap revision against its canonical calculation hash."""
    from src.kb.research_gaps import ResearchGapStore

    return _safe(
        lambda conn: ResearchGapStore(conn, initialize=False).replay(
            namespace, gap_id, scopes={"knowledge:gaps:read"}
        ),
        required_scope="knowledge:gaps:read",
    )


@mcp.tool()
def register_source_capability(
    namespace: str,
    source_id: str,
    semantic_version: str,
    coverage: dict[str, Any],
    authority: dict[str, Any],
    access: dict[str, Any],
    latency: dict[str, Any],
    cost: dict[str, Any],
    rate_limits: dict[str, Any],
    query_forms: list[str],
    connector: dict[str, Any],
    dependency_group: str,
    supersedes_capability_id: str | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Register a credential-safe, immutable source capability version."""
    from src.kb.source_planner import SourcePlannerStore

    return _safe(
        lambda conn: SourcePlannerStore(conn).register_capability(
            namespace,
            source_id,
            semantic_version,
            coverage=coverage,
            authority=authority,
            access=access,
            latency=latency,
            cost=cost,
            rate_limits=rate_limits,
            query_forms=query_forms,
            connector=connector,
            dependency_group=dependency_group,
            supersedes_capability_id=supersedes_capability_id,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy=policy,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:source-planner:write"},
        ),
        write=True,
        required_scope="knowledge:source-planner:write",
    )


@mcp.tool()
def get_source_capability(namespace: str, capability_id: str) -> dict:
    """Inspect one source capability without resolving or exposing credentials."""
    from src.kb.source_planner import SourcePlannerStore

    return _safe(
        lambda conn: SourcePlannerStore(conn, initialize=False).capability(
            namespace, capability_id, scopes={"knowledge:source-planner:read"}
        ),
        required_scope="knowledge:source-planner:read",
    )


@mcp.tool()
def create_source_research_objective(
    namespace: str,
    question: str,
    decomposition: list[dict[str, Any]] | None = None,
    evidence_classes: list[str] | None = None,
    constraints: dict[str, Any] | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Create a canonical decomposed objective with evidence, access, and budget constraints."""
    from src.kb.source_planner import SourcePlannerStore

    return _safe(
        lambda conn: SourcePlannerStore(conn).create_objective(
            namespace,
            question,
            decomposition or [],
            evidence_classes or [],
            constraints,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy=policy,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:source-planner:write"},
        ),
        write=True,
        required_scope="knowledge:source-planner:write",
    )


@mcp.tool()
def preview_source_acquisition_plan(
    namespace: str, objective_id: str, at_ms: int
) -> dict:
    """Preview deterministic source selection without persisting a plan or resolving secrets."""
    from src.kb.source_planner import SourcePlannerStore

    return _safe(
        lambda conn: SourcePlannerStore(conn, initialize=False).preview(
            namespace,
            objective_id,
            at_ms=at_ms,
            credential_available=lambda ref: bool(_secret_resolver(ref)),
            scopes={"knowledge:source-planner:read"},
        ),
        required_scope="knowledge:source-planner:read",
    )


@mcp.tool()
def create_source_acquisition_plan(
    namespace: str, objective_id: str, at_ms: int
) -> dict:
    """Persist an explainable plan pinned to exact source capability versions."""
    from src.kb.source_planner import SourcePlannerStore

    return _safe(
        lambda conn: SourcePlannerStore(conn).preview(
            namespace,
            objective_id,
            at_ms=at_ms,
            credential_available=lambda ref: bool(_secret_resolver(ref)),
            persist=True,
            principal_id=_context()[0],
            scopes={"knowledge:source-planner:write"},
        ),
        write=True,
        required_scope="knowledge:source-planner:write",
    )


@mcp.tool()
def get_source_acquisition_plan(namespace: str, plan_id: str) -> dict:
    """Read an exact persisted source plan."""
    from src.kb.source_planner import SourcePlannerStore

    return _safe(
        lambda conn: SourcePlannerStore(conn, initialize=False).plan(
            namespace, plan_id, scopes={"knowledge:source-planner:read"}
        ),
        required_scope="knowledge:source-planner:read",
    )


@mcp.tool()
def explain_source_acquisition_plan(namespace: str, plan_id: str) -> dict:
    """Explain selected sources, score components, exclusions, coverage, and feasibility."""
    from src.kb.source_planner import SourcePlannerStore

    def operation(conn):
        plan = SourcePlannerStore(conn, initialize=False).plan(
            namespace, plan_id, scopes={"knowledge:source-planner:read"}
        )
        return {
            "plan_id": plan_id,
            "feasible": plan["feasible"],
            "infeasibility": plan["infeasibility"],
            "coverage": plan["coverage"],
            "budget": plan["budget"],
            "steps": plan["steps"],
            "fallback_steps": plan["fallback_steps"],
            "exclusions": plan["exclusions"],
            "plan_hash": plan["plan_hash"],
        }

    return _safe(operation, required_scope="knowledge:source-planner:read")


@mcp.tool()
def execute_source_acquisition_plan(
    namespace: str,
    plan_id: str,
    execution_key: str,
    live_network: bool = False,
) -> dict:
    """Run or resume a source plan through pinned source-pack connectors and checkpoints."""
    from src.kb.source_planner import SourcePlannerStore, source_plan_run_id

    run_id = source_plan_run_id(namespace, plan_id, execution_key)
    event = _SOURCE_PLAN_CANCELLATIONS.setdefault(run_id, threading.Event())

    def operation(conn):
        runtime = _source_pack_runtime(conn, initialize=True)
        plan = SourcePlannerStore(conn, initialize=False).plan(
            namespace, plan_id, scopes={"knowledge:source-planner:read"}
        )

        def runner(capability, step, checkpoint):
            connector = capability["connector"]
            if connector.get("kind") != "source-pack":
                from src.kb.source_planner import SourcePlannerError

                raise SourcePlannerError(
                    "connector_unsupported",
                    "execution requires a source-pack connector",
                )
            query = step["queries"][0]
            requested = {
                "pack_id": connector["pack_id"],
                "run_key": f"{execution_key}:{step['step_id']}",
                "operation": query["query_form"],
                "source_ids": [connector.get("source_id", capability["source_id"])],
                "parameters": query.get("parameters") or {"query": query["question"]},
                "redistribute": bool(plan["constraints"]["redistribute"]),
                "network": "live" if live_network else "disabled",
                "max_pages": min(int(plan["constraints"]["max_pages"]), 100),
                "max_results": min(int(plan["constraints"]["max_results"]), 100_000),
                "timeout_ms": min(int(plan["constraints"]["timeout_ms"]), 120_000),
                "retries": min(int(plan["constraints"]["retries"]), 3),
            }
            result = runtime.run(
                requested,
                principal_id=_context()[0],
                adapters=None
                if live_network
                else runtime.fixture_adapters(connector["pack_id"], ROOT),
                secret_resolver=_secret_resolver,
                dns_resolver=None if live_network else lambda _: ["8.8.8.8"],
                cancelled=event.is_set,
            )
            sources = result.get("sources", [])
            counts = sources[0].get("counts", {}) if sources else {}
            cursor = sources[0].get("cursor", {}) if sources else {}
            return {
                "status": "completed"
                if result.get("status") == "complete"
                else "failed",
                "counts": counts,
                "cursor": cursor,
                "cost": step["projected_cost"],
                "error": None
                if result.get("status") == "complete"
                else {"code": "source-pack-partial", "message": result.get("status")},
            }

        return SourcePlannerStore(conn).execute(
            namespace,
            plan_id,
            execution_key,
            runner=runner,
            cancelled=event.is_set,
            principal_id=_context()[0],
            scopes={"knowledge:source-planner:execute"},
        )

    try:
        return _safe(
            operation,
            write=True,
            required_scope="knowledge:source-planner:execute",
        )
    finally:
        _SOURCE_PLAN_CANCELLATIONS.pop(run_id, None)


@mcp.tool()
def cancel_source_acquisition_plan(
    namespace: str, plan_id: str, execution_key: str
) -> dict:
    """Cooperatively cancel an active source acquisition plan."""
    from src.kb.source_planner import source_plan_run_id

    if (
        "knowledge:source-planner:execute" not in _context()[1]
        and "operator" not in _context()[1]
    ):
        return {
            "ok": False,
            "error": {
                "code": "unauthorized",
                "message": "knowledge:source-planner:execute scope is required",
            },
        }
    run_id = source_plan_run_id(namespace, plan_id, execution_key)
    event = _SOURCE_PLAN_CANCELLATIONS.get(run_id)
    if event is None:
        return {
            "ok": False,
            "error": {"code": "run_not_active", "message": "source plan is not active"},
        }
    event.set()
    return {"ok": True, "run_id": run_id, "cancelled": True}


@mcp.tool()
def inspect_source_acquisition_run(namespace: str, run_id: str) -> dict:
    """Inspect durable status, checkpoints, failures, and budget accounting."""
    from src.kb.source_planner import SourcePlannerStore

    return _safe(
        lambda conn: SourcePlannerStore(conn, initialize=False).inspect_run(
            namespace, run_id, scopes={"knowledge:source-planner:read"}
        ),
        required_scope="knowledge:source-planner:read",
    )


@mcp.tool()
def replay_source_acquisition_run(namespace: str, run_id: str) -> dict:
    """Verify a completed source-plan receipt against its canonical hash."""
    from src.kb.source_planner import SourcePlannerStore

    return _safe(
        lambda conn: SourcePlannerStore(conn, initialize=False).replay(
            namespace, run_id, scopes={"knowledge:source-planner:read"}
        ),
        required_scope="knowledge:source-planner:read",
    )


@mcp.tool()
def register_dataset_catalog(
    namespace: str,
    publisher_id: str,
    native_id: str,
    semantic_version: str,
    title: str,
    description: str,
    license: dict[str, Any],
    tables: list[dict[str, Any]],
    code_lists: list[dict[str, Any]] | None = None,
    partitions: list[dict[str, Any]] | None = None,
    predecessor_revision_id: str | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Register stable dataset, table, column, dimension, and code-list identities."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn).register_dataset(
            namespace,
            publisher_id,
            native_id,
            semantic_version,
            title,
            description,
            license,
            tables,
            code_lists or [],
            partitions or [],
            predecessor_revision_id=predecessor_revision_id,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy=policy,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:dataset:write"},
        ),
        write=True,
        required_scope="knowledge:dataset:write",
    )


@mcp.tool()
def get_dataset_catalog(
    namespace: str, dataset_id: str, revision_id: str | None = None
) -> dict:
    """Inspect a current or exact versioned dataset schema."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn, initialize=False).dataset(
            namespace,
            dataset_id,
            revision_id=revision_id,
            scopes={"knowledge:dataset:read"},
        ),
        required_scope="knowledge:dataset:read",
    )


@mcp.tool()
def search_datasets(
    namespace: str, query: str, limit: int = 50, offset: int = 0
) -> dict:
    """Search dataset, table, and column metadata with bounded pagination."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn, initialize=False).search(
            namespace,
            query,
            limit=limit,
            offset=offset,
            scopes={"knowledge:dataset:read"},
        ),
        required_scope="knowledge:dataset:read",
    )


@mcp.tool()
def register_dataset_release(
    namespace: str,
    dataset_id: str,
    native_release_id: str,
    vintage_id: str,
    retrieved_at_ms: int,
    revision_of: str | None = None,
    published_at_ms: int | None = None,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    generation: int = 0,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Register an immutable dataset release, correction, and vintage."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn).register_release(
            namespace,
            dataset_id,
            native_release_id,
            vintage_id,
            retrieved_at_ms=retrieved_at_ms,
            revision_of=revision_of,
            published_at_ms=published_at_ms,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            generation=generation,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:dataset:write"},
        ),
        write=True,
        required_scope="knowledge:dataset:write",
    )


@mcp.tool()
def get_dataset_release(namespace: str, release_id: str) -> dict:
    """Read a release with its pinned catalog revision and provenance."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn, initialize=False).release(
            namespace, release_id, scopes={"knowledge:dataset:read"}
        ),
        required_scope="knowledge:dataset:read",
    )


@mcp.tool()
def ingest_tabular_dataset(
    namespace: str,
    release_id: str,
    table_id: str,
    format: str,
    content: str,
    partition_key: dict[str, Any] | None = None,
    encoding: str = "utf-8",
    row_limit: int = 10_000,
    inference_limit: int = 1_000,
    cancel_requested: bool = False,
) -> dict:
    """Ingest bounded CSV, JSON, JSONL, Parquet, or tabular API rows."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn).ingest(
            namespace,
            release_id,
            table_id,
            format,
            content,
            partition_key or {},
            encoding=encoding,
            row_limit=row_limit,
            inference_limit=inference_limit,
            cancel_requested=cancel_requested,
            principal_id=_context()[0],
            scopes={"knowledge:dataset:ingest"},
        ),
        write=True,
        required_scope="knowledge:dataset:ingest",
    )


@mcp.tool()
def replay_tabular_ingestion(namespace: str, receipt_id: str) -> dict:
    """Replay the deterministic output chain of a tabular ingestion receipt."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn, initialize=False).replay_ingestion(
            namespace, receipt_id, scopes={"knowledge:dataset:read"}
        ),
        required_scope="knowledge:dataset:read",
    )


@mcp.tool()
def slice_dataset_table(
    namespace: str,
    release_id: str,
    table_id: str,
    partition_key: dict[str, Any] | None = None,
    offset: int = 0,
    limit: int = 100,
    columns: list[str] | None = None,
) -> dict:
    """Read a bounded, vintage-pinned table slice with explicit null semantics."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn, initialize=False).slice(
            namespace,
            release_id,
            table_id,
            partition_key=partition_key,
            offset=offset,
            limit=limit,
            columns=columns,
            scopes={"knowledge:dataset:read"},
        ),
        required_scope="knowledge:dataset:read",
    )


@mcp.tool()
def compare_dataset_releases(
    namespace: str,
    earlier_release_id: str,
    later_release_id: str,
    table_id: str,
    limit: int = 1_000,
) -> dict:
    """Compare row/cell values and null semantics across exact vintages."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn, initialize=False).compare_releases(
            namespace,
            earlier_release_id,
            later_release_id,
            table_id,
            limit=limit,
            scopes={"knowledge:dataset:read"},
        ),
        required_scope="knowledge:dataset:read",
    )


@mcp.tool()
def suggest_dataset_joins(
    namespace: str,
    left_dataset_id: str,
    right_dataset_id: str,
    limit: int = 50,
) -> dict:
    """Suggest join keys from names, semantic roles, and shared code lists."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn, initialize=False).suggest_joins(
            namespace,
            left_dataset_id,
            right_dataset_id,
            limit=limit,
            scopes={"knowledge:dataset:read"},
        ),
        required_scope="knowledge:dataset:read",
    )


@mcp.tool()
def preview_dataset_join(
    namespace: str,
    left_release_id: str,
    right_release_id: str,
    left_table_id: str,
    right_table_id: str,
    keys: list[dict[str, str]],
    limit: int = 100,
) -> dict:
    """Preview bounded join results, cardinality, and unit/time mismatch warnings."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn, initialize=False).preview_join(
            namespace,
            left_release_id,
            right_release_id,
            left_table_id,
            right_table_id,
            keys,
            limit=limit,
            scopes={"knowledge:dataset:calculate"},
        ),
        required_scope="knowledge:dataset:calculate",
    )


@mcp.tool()
def accept_dataset_join(namespace: str, preview: dict[str, Any]) -> dict:
    """Accept an unchanged join preview and persist exact transformation lineage."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn).accept_join(
            namespace,
            preview,
            principal_id=_context()[0],
            scopes={"knowledge:dataset:write"},
        ),
        write=True,
        required_scope="knowledge:dataset:write",
    )


@mcp.tool()
def get_dataset_lineage(namespace: str, transformation_id: str) -> dict:
    """Trace a derived table to exact releases, tables, keys, and transformation."""
    from src.kb.dataset_intelligence import DatasetIntelligenceStore

    return _safe(
        lambda conn: DatasetIntelligenceStore(conn, initialize=False).lineage(
            namespace, transformation_id, scopes={"knowledge:dataset:read"}
        ),
        required_scope="knowledge:dataset:read",
    )


@mcp.tool()
def register_methodology_study(
    namespace: str,
    external_id: str,
    version: str,
    title: str,
    design: dict[str, Any],
    population: dict[str, Any],
    interventions: list[dict[str, Any]],
    comparators: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    datasets: list[dict[str, Any]] | None = None,
    samples: list[dict[str, Any]] | None = None,
    instruments: list[dict[str, Any]] | None = None,
    analysis_plans: list[dict[str, Any]] | None = None,
    predecessor_revision_id: str | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Register a versioned study and its design, population, and analysis objects."""
    from src.kb.methodology_provenance import MethodologyStore

    return _safe(
        lambda conn: MethodologyStore(conn).register_study(
            namespace,
            external_id,
            version,
            title,
            design,
            population,
            interventions,
            comparators,
            outcomes,
            datasets=datasets or [],
            samples=samples or [],
            instruments=instruments or [],
            analysis_plans=analysis_plans or [],
            predecessor_revision_id=predecessor_revision_id,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy=policy,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:methodology:write"},
        ),
        write=True,
        required_scope="knowledge:methodology:write",
    )


@mcp.tool()
def get_methodology_study(
    namespace: str, study_id: str, revision_id: str | None = None
) -> dict:
    """Read the current or an exact study-method revision."""
    from src.kb.methodology_provenance import MethodologyStore

    return _safe(
        lambda conn: MethodologyStore(conn, initialize=False).study(
            namespace,
            study_id,
            revision_id=revision_id,
            scopes={"knowledge:methodology:read"},
        ),
        required_scope="knowledge:methodology:read",
    )


@mcp.tool()
def search_methodology_studies(
    namespace: str, query: str, limit: int = 50, offset: int = 0
) -> dict:
    """Search current study-method records with bounded pagination."""
    from src.kb.methodology_provenance import MethodologyStore

    return _safe(
        lambda conn: MethodologyStore(conn, initialize=False).search(
            namespace,
            query,
            limit=limit,
            offset=offset,
            scopes={"knowledge:methodology:read"},
        ),
        required_scope="knowledge:methodology:read",
    )


@mcp.tool()
def extract_methodology_statements(
    namespace: str,
    study_id: str,
    document_id: str,
    statements: list[dict[str, Any]],
    limit: int = 500,
    cancel_requested: bool = False,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Persist bounded method statements with page, section, table, or passage locators."""
    from src.kb.methodology_provenance import MethodologyStore

    return _safe(
        lambda conn: MethodologyStore(conn).extract(
            namespace,
            study_id,
            document_id,
            statements,
            limit=limit,
            cancel_requested=cancel_requested,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:methodology:extract"},
        ),
        write=True,
        required_scope="knowledge:methodology:extract",
    )


@mcp.tool()
def replay_methodology_extraction(namespace: str, extraction_id: str) -> dict:
    """Verify the deterministic output hash of an exact-locator extraction."""
    from src.kb.methodology_provenance import MethodologyStore

    return _safe(
        lambda conn: MethodologyStore(conn, initialize=False).replay_extraction(
            namespace, extraction_id, scopes={"knowledge:methodology:read"}
        ),
        required_scope="knowledge:methodology:read",
    )


@mcp.tool()
def assess_methodology_limitation(
    namespace: str,
    study_id: str,
    framework: str,
    dimension: str,
    rationale: str,
    rating: str | None = None,
    evidence_statement_ids: list[str] | None = None,
    applicability: dict[str, Any] | None = None,
    reviewer_id: str | None = None,
    source_locator: dict[str, Any] | None = None,
    observed_at_ms: int | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Record a sourced or reviewed, versioned bias or applicability assessment."""
    from src.kb.methodology_provenance import MethodologyStore

    return _safe(
        lambda conn: MethodologyStore(conn).assess(
            namespace,
            study_id,
            framework,
            dimension,
            rating,
            rationale,
            evidence_statement_ids=evidence_statement_ids or [],
            applicability=applicability,
            reviewer_id=reviewer_id,
            source_locator=source_locator,
            observed_at_ms=observed_at_ms,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:methodology:review"},
        ),
        write=True,
        required_scope="knowledge:methodology:review",
    )


@mcp.tool()
def list_methodology_limitations(
    namespace: str,
    study_id: str,
    framework: str | None = None,
    rating: str | None = None,
    limit: int = 100,
) -> dict:
    """Filter current bias, confounding, power, measurement, and applicability records."""
    from src.kb.methodology_provenance import MethodologyStore

    return _safe(
        lambda conn: MethodologyStore(conn, initialize=False).limitations(
            namespace,
            study_id,
            framework=framework,
            rating=rating,
            limit=limit,
            scopes={"knowledge:methodology:read"},
        ),
        required_scope="knowledge:methodology:read",
    )


@mcp.tool()
def link_study_artifact(
    namespace: str,
    study_id: str,
    artifact_type: str,
    artifact_id: str,
    relation: str,
    status: str = "available",
    version: str | None = None,
    locator: str | None = None,
    indirect_via: str | None = None,
    study_external_id: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Link a study to registrations, protocols, data, code, replications, or corrections."""
    from src.kb.methodology_provenance import MethodologyStore

    return _safe(
        lambda conn: MethodologyStore(conn).link_artifact(
            namespace,
            study_id,
            artifact_type,
            artifact_id,
            relation,
            status=status,
            version=version,
            locator=locator,
            indirect_via=indirect_via,
            study_external_id=study_external_id,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:methodology:write"},
        ),
        write=True,
        required_scope="knowledge:methodology:write",
    )


@mcp.tool()
def get_study_replication_graph(
    namespace: str, study_id: str, limit: int = 100
) -> dict:
    """Read bounded registration, replication, data, code, and correction links."""
    from src.kb.methodology_provenance import MethodologyStore

    return _safe(
        lambda conn: MethodologyStore(conn, initialize=False).replication_graph(
            namespace, study_id, limit=limit, scopes={"knowledge:methodology:read"}
        ),
        required_scope="knowledge:methodology:read",
    )


@mcp.tool()
def compare_study_methodologies(
    namespace: str, study_ids: list[str], limit: int = 20
) -> dict:
    """Compare exact study designs, populations, interventions, outcomes, and plans."""
    from src.kb.methodology_provenance import MethodologyStore

    return _safe(
        lambda conn: MethodologyStore(conn, initialize=False).compare(
            namespace, study_ids, limit=limit, scopes={"knowledge:methodology:read"}
        ),
        required_scope="knowledge:methodology:read",
    )


@mcp.tool()
def explain_study_evidence_strength(namespace: str, study_id: str) -> dict:
    """Explain evidence strength without converting unknown assessments into ratings."""
    from src.kb.methodology_provenance import MethodologyStore

    return _safe(
        lambda conn: MethodologyStore(conn, initialize=False).explain_strength(
            namespace, study_id, scopes={"knowledge:methodology:read"}
        ),
        required_scope="knowledge:methodology:read",
    )


@mcp.tool()
def register_multimodal_asset(
    namespace: str,
    source_id: str,
    native_id: str,
    version: str,
    asset_type: str,
    media_type: str,
    bytes_base64: str | None = None,
    perceptual_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
    segments: list[dict[str, Any]] | None = None,
    predecessor_revision_id: str | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Register a bounded, versioned image, chart, map, audio, video, or page."""
    from src.kb.multimodal_evidence import MultimodalStore

    return _safe(
        lambda conn: MultimodalStore(conn).register_asset(
            namespace,
            source_id,
            native_id,
            version,
            asset_type,
            media_type,
            bytes_base64=bytes_base64,
            perceptual_hash=perceptual_hash,
            metadata=metadata,
            segments=segments or [],
            predecessor_revision_id=predecessor_revision_id,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy=policy,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:multimodal:write"},
        ),
        write=True,
        required_scope="knowledge:multimodal:write",
    )


@mcp.tool()
def get_multimodal_asset(
    namespace: str, asset_id: str, revision_id: str | None = None
) -> dict:
    """Read asset metadata and resolvable segment locators without returning bytes."""
    from src.kb.multimodal_evidence import MultimodalStore

    return _safe(
        lambda conn: MultimodalStore(conn, initialize=False).asset(
            namespace,
            asset_id,
            revision_id=revision_id,
            scopes={"knowledge:multimodal:read"},
        ),
        required_scope="knowledge:multimodal:read",
    )


@mcp.tool()
def search_multimodal_assets(
    namespace: str,
    query: str,
    asset_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Search bounded multimodal metadata within one namespace."""
    from src.kb.multimodal_evidence import MultimodalStore

    return _safe(
        lambda conn: MultimodalStore(conn, initialize=False).search(
            namespace,
            query,
            asset_type=asset_type,
            limit=limit,
            offset=offset,
            scopes={"knowledge:multimodal:read"},
        ),
        required_scope="knowledge:multimodal:read",
    )


@mcp.tool()
def get_multimodal_segment(namespace: str, asset_id: str, segment_id: str) -> dict:
    """Resolve an exact page, region, time segment, or frame evidence locator."""
    from src.kb.multimodal_evidence import MultimodalStore

    return _safe(
        lambda conn: MultimodalStore(conn, initialize=False).segment(
            namespace, asset_id, segment_id, scopes={"knowledge:multimodal:read"}
        ),
        required_scope="knowledge:multimodal:read",
    )


@mcp.tool()
def extract_multimodal_observations(
    namespace: str,
    asset_id: str,
    extractor: str,
    observations: list[dict[str, Any]],
    codec: str | None = None,
    limit: int = 500,
    duration_limit_ms: int = 3_600_000,
    cancel_requested: bool = False,
    adapter: str = "local-fixture-v1",
) -> dict:
    """Persist a bounded local OCR, speech, frame, caption, or chart adapter result."""
    from src.kb.multimodal_evidence import MultimodalStore

    return _safe(
        lambda conn: MultimodalStore(conn).extract(
            namespace,
            asset_id,
            extractor,
            observations,
            codec=codec,
            limit=limit,
            duration_limit_ms=duration_limit_ms,
            cancel_requested=cancel_requested,
            adapter=adapter,
            principal_id=_context()[0],
            scopes={"knowledge:multimodal:extract"},
        ),
        write=True,
        required_scope="knowledge:multimodal:extract",
    )


@mcp.tool()
def replay_multimodal_extraction(namespace: str, extraction_id: str) -> dict:
    """Verify a multimodal extraction receipt against its observation hash."""
    from src.kb.multimodal_evidence import MultimodalStore

    return _safe(
        lambda conn: MultimodalStore(conn, initialize=False).replay(
            namespace, extraction_id, scopes={"knowledge:multimodal:read"}
        ),
        required_scope="knowledge:multimodal:read",
    )


@mcp.tool()
def link_cross_modal_evidence(
    namespace: str,
    observation_id: str,
    target_type: str,
    target_id: str,
    relation: str,
    stance: str,
    confidence: float,
) -> dict:
    """Link an unverified media observation to a claim, entity, event, or source."""
    from src.kb.multimodal_evidence import MultimodalStore

    return _safe(
        lambda conn: MultimodalStore(conn).link_observation(
            namespace,
            observation_id,
            target_type,
            target_id,
            relation,
            stance,
            confidence,
            principal_id=_context()[0],
            scopes={"knowledge:multimodal:write"},
        ),
        write=True,
        required_scope="knowledge:multimodal:write",
    )


@mcp.tool()
def record_media_transformation(
    namespace: str,
    parent_asset_id: str,
    child_asset_id: str,
    operation: str,
    parameters: dict[str, Any],
) -> dict:
    """Record crops, edits, mirrors, and recompression as provenance edges."""
    from src.kb.multimodal_evidence import MultimodalStore

    return _safe(
        lambda conn: MultimodalStore(conn).transform(
            namespace,
            parent_asset_id,
            child_asset_id,
            operation,
            parameters,
            principal_id=_context()[0],
            scopes={"knowledge:multimodal:write"},
        ),
        write=True,
        required_scope="knowledge:multimodal:write",
    )


@mcp.tool()
def assess_media_authenticity(
    namespace: str,
    asset_id: str,
    finding: str,
    confidence: float,
    c2pa: dict[str, Any] | None = None,
    metadata_findings: list[Any] | None = None,
    synthetic_indicators: list[Any] | None = None,
    uncertainty: str | None = None,
    evidence: list[Any] | None = None,
) -> dict:
    """Record a reviewed authenticity finding with calibrated uncertainty."""
    from src.kb.multimodal_evidence import MultimodalStore

    return _safe(
        lambda conn: MultimodalStore(conn).assess_authenticity(
            namespace,
            asset_id,
            finding,
            confidence,
            c2pa=c2pa,
            metadata_findings=metadata_findings or [],
            synthetic_indicators=synthetic_indicators or [],
            uncertainty=uncertainty,
            evidence=evidence or [],
            principal_id=_context()[0],
            scopes={"knowledge:multimodal:review"},
        ),
        write=True,
        required_scope="knowledge:multimodal:review",
    )


@mcp.tool()
def inspect_media_provenance(namespace: str, asset_id: str, limit: int = 100) -> dict:
    """Inspect acquisition hashes, perceptual matches, transformations, and findings."""
    from src.kb.multimodal_evidence import MultimodalStore

    return _safe(
        lambda conn: MultimodalStore(conn, initialize=False).provenance(
            namespace, asset_id, limit=limit, scopes={"knowledge:multimodal:read"}
        ),
        required_scope="knowledge:multimodal:read",
    )


@mcp.tool()
def register_citation_archive_policy(
    namespace: str,
    policy_id: str,
    version: str,
    allow_robots_denied: bool = False,
    allowed_licenses: list[str] | None = None,
    allow_private: bool = False,
    preserve_excerpts: bool = True,
    preserve_assets: bool = False,
    approved_archives: list[str] | None = None,
    max_bytes: int = 1_000_000,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy_context: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    predecessor_revision_id: str | None = None,
) -> dict:
    """Register an append-only legal, access, and citation archive policy."""
    from src.kb.citation_preservation import CitationPreservationStore

    return _safe(
        lambda conn: CitationPreservationStore(conn).register_policy(
            namespace,
            policy_id,
            version,
            allow_robots_denied=allow_robots_denied,
            allowed_licenses=allowed_licenses or [],
            allow_private=allow_private,
            preserve_excerpts=preserve_excerpts,
            preserve_assets=preserve_assets,
            approved_archives=approved_archives or [],
            max_bytes=max_bytes,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy_context=policy_context,
            provenance=provenance,
            predecessor_revision_id=predecessor_revision_id,
            principal_id=_context()[0],
            scopes={"knowledge:citation:write"},
        ),
        write=True,
        required_scope="knowledge:citation:write",
    )


@mcp.tool()
def get_citation_archive_policy(
    namespace: str, policy_id: str, revision_id: str | None = None
) -> dict:
    """Read a current or exact citation archive policy revision."""
    from src.kb.citation_preservation import CitationPreservationStore

    return _safe(
        lambda conn: CitationPreservationStore(conn, initialize=False).policy(
            namespace,
            policy_id,
            revision_id=revision_id,
            scopes={"knowledge:citation:read"},
        ),
        required_scope="knowledge:citation:read",
    )


@mcp.tool()
def capture_citation_snapshot(
    namespace: str,
    policy_id: str,
    citation_id: str,
    source_url: str,
    content: str | None = None,
    media_type: str = "text/html",
    retrieved_at_ms: int | None = None,
    redirects: list[str] | None = None,
    response_metadata: dict[str, Any] | None = None,
    locator: dict[str, Any] | None = None,
    excerpts: list[dict[str, Any]] | None = None,
    assets: list[dict[str, Any]] | None = None,
    robots_allowed: bool = True,
    license_id: str | None = None,
    private_source: bool = False,
    partial: bool = False,
    cancel_requested: bool = False,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Capture a bounded, policy-gated, content-addressed citation manifest."""
    from src.kb.citation_preservation import CitationPreservationStore

    return _safe(
        lambda conn: CitationPreservationStore(conn).capture(
            namespace,
            policy_id,
            citation_id,
            source_url,
            content=content,
            media_type=media_type,
            retrieved_at_ms=retrieved_at_ms,
            redirects=redirects or [],
            response_metadata=response_metadata,
            locator=locator,
            excerpts=excerpts or [],
            assets=assets or [],
            robots_allowed=robots_allowed,
            license_id=license_id,
            private_source=private_source,
            partial=partial,
            cancel_requested=cancel_requested,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            producer=producer,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:citation:capture"},
        ),
        write=True,
        required_scope="knowledge:citation:capture",
    )


@mcp.tool()
def get_citation_snapshot(namespace: str, snapshot_id: str) -> dict:
    """Inspect a citation manifest without returning preserved content bytes."""
    from src.kb.citation_preservation import CitationPreservationStore

    return _safe(
        lambda conn: CitationPreservationStore(conn, initialize=False).snapshot(
            namespace, snapshot_id, scopes={"knowledge:citation:read"}
        ),
        required_scope="knowledge:citation:read",
    )


@mcp.tool()
def replay_citation_snapshot(namespace: str, snapshot_id: str) -> dict:
    """Verify a snapshot manifest against its deterministic hash."""
    from src.kb.citation_preservation import CitationPreservationStore

    return _safe(
        lambda conn: CitationPreservationStore(conn, initialize=False).replay_capture(
            namespace, snapshot_id, scopes={"knowledge:citation:read"}
        ),
        required_scope="knowledge:citation:read",
    )


@mcp.tool()
def verify_preserved_citation(
    namespace: str,
    citation_id: str,
    snapshot_id: str,
    assertion: str,
    expected_excerpt: str | None = None,
    contradiction: str | None = None,
    locator: dict[str, Any] | None = None,
    ocr_tolerance: float = 0.85,
) -> dict:
    """Re-resolve a cited passage and report support, contradiction, ambiguity, or loss."""
    from src.kb.citation_preservation import CitationPreservationStore

    return _safe(
        lambda conn: CitationPreservationStore(conn).verify(
            namespace,
            citation_id,
            snapshot_id,
            assertion,
            expected_excerpt=expected_excerpt,
            contradiction=contradiction,
            locator=locator,
            ocr_tolerance=ocr_tolerance,
            principal_id=_context()[0],
            scopes={"knowledge:citation:write"},
        ),
        write=True,
        required_scope="knowledge:citation:write",
    )


@mcp.tool()
def record_citation_health(
    namespace: str,
    citation_id: str,
    url: str,
    http_status: int,
    response_title: str = "",
    paywall: bool = False,
    takedown: bool = False,
    checked_at_ms: int | None = None,
) -> dict:
    """Record availability, soft-404, paywall, or takedown status."""
    from src.kb.citation_preservation import CitationPreservationStore

    return _safe(
        lambda conn: CitationPreservationStore(conn).record_health(
            namespace,
            citation_id,
            url,
            http_status,
            response_title=response_title,
            paywall=paywall,
            takedown=takedown,
            checked_at_ms=checked_at_ms,
            principal_id=_context()[0],
            scopes={"knowledge:citation:write"},
        ),
        write=True,
        required_scope="knowledge:citation:write",
    )


@mcp.tool()
def get_citation_status(namespace: str, citation_id: str, limit: int = 100) -> dict:
    """Inspect bounded snapshots, availability checks, and explicit repairs."""
    from src.kb.citation_preservation import CitationPreservationStore

    return _safe(
        lambda conn: CitationPreservationStore(conn, initialize=False).status(
            namespace, citation_id, limit=limit, scopes={"knowledge:citation:read"}
        ),
        required_scope="knowledge:citation:read",
    )


@mcp.tool()
def preview_citation_repair(
    namespace: str,
    policy_id: str,
    citation_id: str,
    snapshot_id: str,
    candidates: list[dict[str, Any]],
    limit: int = 20,
) -> dict:
    """Preview approved archive candidates without changing original evidence."""
    from src.kb.citation_preservation import CitationPreservationStore

    return _safe(
        lambda conn: CitationPreservationStore(conn, initialize=False).preview_repair(
            namespace,
            policy_id,
            citation_id,
            snapshot_id,
            candidates,
            limit=limit,
            scopes={"knowledge:citation:read"},
        ),
        required_scope="knowledge:citation:read",
    )


@mcp.tool()
def accept_citation_repair(
    namespace: str, preview: dict[str, Any], candidate_index: int
) -> dict:
    """Record an approved exact-content archived copy while retaining the original."""
    from src.kb.citation_preservation import CitationPreservationStore

    return _safe(
        lambda conn: CitationPreservationStore(conn).accept_repair(
            namespace,
            preview,
            candidate_index,
            principal_id=_context()[0],
            scopes={"knowledge:citation:repair"},
        ),
        write=True,
        required_scope="knowledge:citation:repair",
    )


@mcp.tool()
def export_preserved_citations(
    namespace: str, citation_ids: list[str], limit: int = 100
) -> dict:
    """Export citation manifests with policy, verification, health, and repair closure."""
    from src.kb.citation_preservation import CitationPreservationStore

    return _safe(
        lambda conn: CitationPreservationStore(conn, initialize=False).export(
            namespace, citation_ids, limit=limit, scopes={"knowledge:citation:read"}
        ),
        required_scope="knowledge:citation:read",
    )


@mcp.tool()
def register_change_brief_policy(
    namespace: str,
    policy_id: str,
    version: str,
    weights: dict[str, Any] | None = None,
    minimum_score: float = 0.25,
    user_priorities: dict[str, Any] | None = None,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy_context: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Register an immutable material-change ranking policy."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c).register_policy(
            namespace,
            policy_id,
            version,
            weights=weights,
            minimum_score=minimum_score,
            user_priorities=user_priorities,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy_context=policy_context,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:briefs:write"},
        ),
        write=True,
        required_scope="knowledge:briefs:write",
    )


@mcp.tool()
def preview_semantic_change(
    namespace: str,
    object_type: str,
    object_id: str,
    before: Any,
    after: Any,
    from_generation: int | None,
    to_generation: int | None,
    evidence_before: list[dict[str, Any]] | None = None,
    evidence_after: list[dict[str, Any]] | None = None,
    factors: dict[str, Any] | None = None,
    coverage_before: bool = True,
    coverage_after: bool = True,
) -> dict:
    """Classify and preview a bounded before/after semantic change."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c, initialize=False).preview(
            namespace,
            object_type,
            object_id,
            before,
            after,
            from_generation,
            to_generation,
            evidence_before=evidence_before or [],
            evidence_after=evidence_after or [],
            factors=factors,
            coverage_before=coverage_before,
            coverage_after=coverage_after,
            scopes={"knowledge:briefs:read"},
        ),
        required_scope="knowledge:briefs:read",
    )


@mcp.tool()
def generate_change_brief(
    namespace: str,
    policy_revision_id: str,
    preview: dict[str, Any],
    cancel_requested: bool = False,
) -> dict:
    """Generate an evidence-linked, policy-ranked semantic change brief."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c).generate(
            namespace,
            policy_revision_id,
            preview,
            cancel_requested=cancel_requested,
            principal_id=_context()[0],
            scopes={"knowledge:briefs:write"},
        ),
        write=True,
        required_scope="knowledge:briefs:write",
    )


@mcp.tool()
def get_change_brief(namespace: str, brief_id: str) -> dict:
    """Read one exact generated change brief."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c, initialize=False).get(
            namespace, brief_id, scopes={"knowledge:briefs:read"}
        ),
        required_scope="knowledge:briefs:read",
    )


@mcp.tool()
def list_change_briefs(
    namespace: str,
    object_type: str | None = None,
    object_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List deterministic, paginated change brief history."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c, initialize=False).history(
            namespace,
            object_type,
            object_id,
            limit=limit,
            offset=offset,
            scopes={"knowledge:briefs:read"},
        ),
        required_scope="knowledge:briefs:read",
    )


@mcp.tool()
def compare_change_briefs(
    namespace: str, left_brief_id: str, right_brief_id: str
) -> dict:
    """Compare classifications, materiality, scores, and uncertainty."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c, initialize=False).compare(
            namespace, left_brief_id, right_brief_id, scopes={"knowledge:briefs:read"}
        ),
        required_scope="knowledge:briefs:read",
    )


@mcp.tool()
def replay_change_brief(namespace: str, brief_id: str) -> dict:
    """Verify a generated change brief against its deterministic hash."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c, initialize=False).replay(
            namespace, brief_id, scopes={"knowledge:briefs:read"}
        ),
        required_scope="knowledge:briefs:read",
    )


@mcp.tool()
def create_change_brief_subscription(
    namespace: str, subscriber_id: str, window_ms: int, filters: dict[str, Any]
) -> dict:
    """Create a subscriber-scoped aggregation window."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c).subscribe(
            namespace,
            subscriber_id,
            window_ms,
            filters,
            principal_id=_context()[0],
            scopes={"knowledge:briefs:write"},
        ),
        write=True,
        required_scope="knowledge:briefs:write",
    )


@mcp.tool()
def deliver_change_briefs(
    namespace: str,
    subscription_id: str,
    window_start_ms: int,
    window_end_ms: int,
    cancel_requested: bool = False,
) -> dict:
    """Build a deduplicated, retry-safe delivery or explicit quiet period."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c).deliver(
            namespace,
            subscription_id,
            window_start_ms,
            window_end_ms,
            cancel_requested=cancel_requested,
            principal_id=_context()[0],
            scopes={"knowledge:briefs:deliver"},
        ),
        write=True,
        required_scope="knowledge:briefs:deliver",
    )


@mcp.tool()
def acknowledge_change_brief_delivery(namespace: str, delivery_id: str) -> dict:
    """Acknowledge a subscriber-scoped brief delivery."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c).acknowledge(
            namespace,
            delivery_id,
            principal_id=_context()[0],
            scopes={"knowledge:briefs:deliver"},
        ),
        write=True,
        required_scope="knowledge:briefs:deliver",
    )


@mcp.tool()
def review_change_brief(
    namespace: str, brief_id: str, rating: str, reason: str
) -> dict:
    """Record principal-scoped feedback on a generated brief."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c).feedback(
            namespace,
            brief_id,
            rating,
            reason,
            principal_id=_context()[0],
            scopes={"knowledge:briefs:review"},
        ),
        write=True,
        required_scope="knowledge:briefs:review",
    )


@mcp.tool()
def export_change_briefs(
    namespace: str, brief_ids: list[str], limit: int = 100
) -> dict:
    """Export briefs with exact policy dependencies and deterministic hashing."""
    from src.kb.change_briefs import ChangeBriefStore

    return _safe(
        lambda c: ChangeBriefStore(c, initialize=False).export(
            namespace, brief_ids, limit=limit, scopes={"knowledge:briefs:read"}
        ),
        required_scope="knowledge:briefs:read",
    )


def _recipe_known_tools() -> set[str]:
    import json

    from src.mcp_host.catalog import CATALOG_ARTIFACT

    try:
        return {
            str(v["name"]) for v in json.loads(CATALOG_ARTIFACT.read_text())["tools"]
        }
    except (OSError, KeyError, ValueError, TypeError):
        return set()


@mcp.tool()
def validate_research_recipe(recipe: dict[str, Any]) -> dict:
    """Validate a typed recipe DAG, compatibility declarations, and canonical hash."""
    from src.kb.research_recipes import validate_recipe

    return _safe(
        lambda _: validate_recipe(recipe, known_tools=_recipe_known_tools()),
        required_scope="knowledge:recipes:read",
    )


@mcp.tool()
def register_research_recipe(recipe: dict[str, Any]) -> dict:
    """Register an immutable research recipe revision."""
    from src.kb.research_recipes import ResearchRecipeStore

    return _safe(
        lambda c: ResearchRecipeStore(c).register(
            recipe,
            principal_id=_context()[0],
            scopes={"knowledge:recipes:write"},
            known_tools=_recipe_known_tools(),
        ),
        write=True,
        required_scope="knowledge:recipes:write",
    )


@mcp.tool()
def list_research_recipes(namespace: str, limit: int = 50, offset: int = 0) -> dict:
    """List registered recipe revisions with bounded pagination."""
    from src.kb.research_recipes import ResearchRecipeStore

    return _safe(
        lambda c: ResearchRecipeStore(c, initialize=False).list(
            namespace, limit=limit, offset=offset, scopes={"knowledge:recipes:read"}
        ),
        required_scope="knowledge:recipes:read",
    )


@mcp.tool()
def preview_research_recipe(
    namespace: str,
    recipe_revision_id: str,
    parameters: dict[str, Any],
    granted_scopes: list[str] | None = None,
    allowed_sources: list[str] | None = None,
    network_allowed: bool = False,
    available_tool_versions: dict[str, str] | None = None,
) -> dict:
    """Preview safe parameters, secret references, compatibility, and policy gates."""
    from src.kb.research_recipes import ResearchRecipeStore

    return _safe(
        lambda c: ResearchRecipeStore(c, initialize=False).preview(
            namespace,
            recipe_revision_id,
            parameters,
            granted_scopes=granted_scopes or [],
            allowed_sources=allowed_sources or [],
            network_allowed=network_allowed,
            available_tool_versions=available_tool_versions,
            scopes={"knowledge:recipes:read"},
        ),
        required_scope="knowledge:recipes:read",
    )


@mcp.tool()
def run_research_recipe(
    namespace: str,
    recipe_revision_id: str,
    parameters: dict[str, Any],
    run_key: str,
    step_outputs: dict[str, dict[str, Any]],
    granted_scopes: list[str] | None = None,
    allowed_sources: list[str] | None = None,
    network_allowed: bool = False,
    tool_versions: dict[str, str] | None = None,
    snapshot_tokens: list[dict[str, Any]] | None = None,
    secrets: dict[str, str] | None = None,
    fail_after: int | None = None,
) -> dict:
    """Run or resume a recipe with bounded local step adapters and durable checkpoints."""
    from src.kb.research_recipes import ResearchRecipeStore

    adapters = {
        name: (lambda step, state, value=value: dict(value))
        for name, value in step_outputs.items()
    }
    return _safe(
        lambda c: ResearchRecipeStore(c).run(
            namespace,
            recipe_revision_id,
            parameters,
            run_key=run_key,
            adapters=adapters,
            principal_id=_context()[0],
            scopes={"knowledge:recipes:execute"},
            secret_resolver=lambda ref: (secrets or {}).get(ref),
            granted_scopes=granted_scopes or [],
            allowed_sources=allowed_sources or [],
            network_allowed=network_allowed,
            tool_versions=tool_versions,
            snapshot_tokens=snapshot_tokens,
            fail_after=fail_after,
        ),
        write=True,
        required_scope="knowledge:recipes:execute",
    )


@mcp.tool()
def get_research_recipe_run(namespace: str, run_id: str) -> dict:
    """Inspect recipe state, checkpoints, errors, and completed receipt."""
    from src.kb.research_recipes import ResearchRecipeStore

    return _safe(
        lambda c: ResearchRecipeStore(c, initialize=False).status(
            namespace, run_id, scopes={"knowledge:recipes:read"}
        ),
        required_scope="knowledge:recipes:read",
    )


@mcp.tool()
def cancel_research_recipe_run(namespace: str, run_id: str) -> dict:
    """Request cooperative cancellation at the next durable checkpoint."""
    from src.kb.research_recipes import ResearchRecipeStore

    return _safe(
        lambda c: ResearchRecipeStore(c).cancel(
            namespace,
            run_id,
            principal_id=_context()[0],
            scopes={"knowledge:recipes:execute"},
        ),
        write=True,
        required_scope="knowledge:recipes:execute",
    )


@mcp.tool()
def replay_research_recipe_run(
    namespace: str, run_id: str, current_tool_versions: dict[str, str] | None = None
) -> dict:
    """Verify outputs, receipt hashing, and pinned tool versions."""
    from src.kb.research_recipes import ResearchRecipeStore

    return _safe(
        lambda c: ResearchRecipeStore(c, initialize=False).replay(
            namespace,
            run_id,
            current_tool_versions=current_tool_versions,
            scopes={"knowledge:recipes:read"},
        ),
        required_scope="knowledge:recipes:read",
    )


@mcp.tool()
def export_research_recipe_run(namespace: str, run_id: str) -> dict:
    """Export an exact recipe revision, run state, checkpoints, and receipt."""
    from src.kb.research_recipes import ResearchRecipeStore

    return _safe(
        lambda c: ResearchRecipeStore(c, initialize=False).export(
            namespace, run_id, scopes={"knowledge:recipes:read"}
        ),
        required_scope="knowledge:recipes:read",
    )


@mcp.tool()
def register_quality_policy(
    namespace: str,
    policy_id: str,
    version: str,
    dimensions: dict[str, dict[str, Any]],
    domain_overrides: dict[str, Any] | None = None,
    calibration: dict[str, Any] | None = None,
    threshold: float = 0.5,
    generation: int = 0,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy_context: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict:
    """Register versioned quality dimensions, defaults, overrides, and calibration."""
    from src.kb.knowledge_quality import QualityStore

    return _safe(
        lambda c: QualityStore(c).register_policy(
            namespace,
            policy_id,
            version,
            dimensions,
            domain_overrides=domain_overrides,
            calibration=calibration,
            threshold=threshold,
            generation=generation,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy_context=policy_context,
            provenance=provenance,
            principal_id=_context()[0],
            scopes={"knowledge:quality:write"},
        ),
        write=True,
        required_scope="knowledge:quality:write",
    )


@mcp.tool()
def get_quality_policy(namespace: str, policy_revision_id: str) -> dict:
    """Read an exact multidimensional quality policy."""
    from src.kb.knowledge_quality import QualityStore

    return _safe(
        lambda c: QualityStore(c, initialize=False).policy(
            namespace, policy_revision_id, scopes={"knowledge:quality:read"}
        ),
        required_scope="knowledge:quality:read",
    )


@mcp.tool()
def assess_knowledge_quality(
    namespace: str,
    object_type: str,
    object_id: str,
    generation: int,
    policy_revision_id: str,
    features: dict[str, Any],
    input_lineage: list[dict[str, Any]],
    domain: str | None = None,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    producer: dict[str, Any] | None = None,
    policy_context: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    cancel_requested: bool = False,
) -> dict:
    """Compute auditable per-dimension features with exact input lineage."""
    from src.kb.knowledge_quality import QualityStore

    return _safe(
        lambda c: QualityStore(c).assess(
            namespace,
            object_type,
            object_id,
            generation,
            policy_revision_id,
            features,
            input_lineage=input_lineage,
            domain=domain,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            producer=producer,
            policy_context=policy_context,
            provenance=provenance,
            cancel_requested=cancel_requested,
            principal_id=_context()[0],
            scopes={"knowledge:quality:calculate"},
        ),
        write=True,
        required_scope="knowledge:quality:calculate",
    )


@mcp.tool()
def get_quality_assessment(namespace: str, assessment_id: str) -> dict:
    """Inspect dimensions, uncertainty, defaults, flags, and lineage."""
    from src.kb.knowledge_quality import QualityStore

    return _safe(
        lambda c: QualityStore(c, initialize=False).get(
            namespace, assessment_id, scopes={"knowledge:quality:read"}
        ),
        required_scope="knowledge:quality:read",
    )


@mcp.tool()
def replay_quality_assessment(namespace: str, assessment_id: str) -> dict:
    """Verify a quality assessment against its deterministic hash."""
    from src.kb.knowledge_quality import QualityStore

    return _safe(
        lambda c: QualityStore(c, initialize=False).replay(
            namespace, assessment_id, scopes={"knowledge:quality:read"}
        ),
        required_scope="knowledge:quality:read",
    )


@mcp.tool()
def aggregate_quality_assessments(
    namespace: str,
    assessment_ids: list[str],
    limit: int = 500,
    calibration_samples: list[float] | None = None,
    reference_distribution: dict[str, Any] | None = None,
) -> dict:
    """Aggregate bounded assessments with correlation and calibration warnings."""
    from src.kb.knowledge_quality import QualityStore

    return _safe(
        lambda c: QualityStore(c, initialize=False).collection(
            namespace,
            assessment_ids,
            limit=limit,
            calibration_samples=calibration_samples,
            reference_distribution=reference_distribution,
            scopes={"knowledge:quality:calculate"},
        ),
        required_scope="knowledge:quality:calculate",
    )


@mcp.tool()
def rank_by_quality(
    namespace: str,
    assessment_ids: list[str],
    threshold: float | None = None,
    descending: bool = True,
    user_overrides: dict[str, float] | None = None,
) -> dict:
    """Rank while retaining low-scored evidence and exposing every dimension."""
    from src.kb.knowledge_quality import QualityStore

    return _safe(
        lambda c: QualityStore(c, initialize=False).rank(
            namespace,
            assessment_ids,
            threshold=threshold,
            descending=descending,
            user_overrides=user_overrides,
            scopes={"knowledge:quality:read"},
        ),
        required_scope="knowledge:quality:read",
    )


@mcp.tool()
def simulate_quality_policy(
    namespace: str, assessment_ids: list[str], policy_revision_id: str
) -> dict:
    """Simulate a policy without modifying stored assessments."""
    from src.kb.knowledge_quality import QualityStore

    return _safe(
        lambda c: QualityStore(c, initialize=False).simulate(
            namespace,
            assessment_ids,
            policy_revision_id,
            scopes={"knowledge:quality:read"},
        ),
        required_scope="knowledge:quality:read",
    )


@mcp.tool()
def compare_quality_policies(
    namespace: str, left_policy_revision_id: str, right_policy_revision_id: str
) -> dict:
    """Compare dimension definitions, weights, defaults, and thresholds."""
    from src.kb.knowledge_quality import QualityStore

    return _safe(
        lambda c: QualityStore(c, initialize=False).compare_policies(
            namespace,
            left_policy_revision_id,
            right_policy_revision_id,
            scopes={"knowledge:quality:read"},
        ),
        required_scope="knowledge:quality:read",
    )


@mcp.tool()
def review_quality_override(
    namespace: str,
    object_id: str,
    dimension: str,
    value: float,
    reason: str,
    reviewer_id: str,
) -> dict:
    """Record an explicit human quality override without rewriting evidence."""
    from src.kb.knowledge_quality import QualityStore

    return _safe(
        lambda c: QualityStore(c).override(
            namespace,
            object_id,
            dimension,
            value,
            reason,
            reviewer_id=reviewer_id,
            principal_id=_context()[0],
            scopes={"knowledge:quality:review"},
        ),
        write=True,
        required_scope="knowledge:quality:review",
    )


@mcp.tool()
def inspect_quality_health(
    namespace: str, assessment_ids: list[str], limit: int = 500
) -> dict:
    """Report coverage gaps and degraded inputs without deleting assessments."""
    from src.kb.knowledge_quality import QualityStore

    return _safe(
        lambda c: QualityStore(c, initialize=False).health(
            namespace, assessment_ids, limit=limit, scopes={"knowledge:quality:read"}
        ),
        required_scope="knowledge:quality:read",
    )


@mcp.tool()
def register_entity_history_identity(
    namespace: str, entity_id: str, aliases: list[str] | None = None
) -> dict:
    """Register an entity identity for reviewed merge and split history."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c).register_entity(
            namespace,
            entity_id,
            aliases or [],
            principal_id=_context()[0],
            scopes={"knowledge:entity-history:write"},
        ),
        write=True,
        required_scope="knowledge:entity-history:write",
    )


@mcp.tool()
def record_entity_identity_decision(
    namespace: str,
    decision_type: str,
    subject_ids: list[str],
    payload: dict[str, Any],
    reviewer_id: str,
    event_key: str | None = None,
) -> dict:
    """Append an alias, match, non-match, redirect, or review decision."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c).decide(
            namespace,
            decision_type,
            subject_ids,
            payload,
            reviewer_id=reviewer_id,
            event_key=event_key,
            principal_id=_context()[0],
            scopes={"knowledge:entity-history:review"},
        ),
        write=True,
        required_scope="knowledge:entity-history:review",
    )


@mcp.tool()
def resolve_entity_history(
    namespace: str, entity_id: str, at_revision: int | None = None
) -> dict:
    """Resolve an entity through current or snapshot-bounded redirects."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c, initialize=False).resolve(
            namespace,
            entity_id,
            at_revision=at_revision,
            scopes={"knowledge:entity-history:read"},
        ),
        required_scope="knowledge:entity-history:read",
    )


@mcp.tool()
def preview_entity_merge(
    namespace: str,
    source_ids: list[str],
    target_id: str,
    dual_control: bool = False,
    approvals: list[str] | None = None,
) -> dict:
    """Preview redirects, cycles, dual control, and downstream impact."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c, initialize=False).merge_preview(
            namespace,
            source_ids,
            target_id,
            dual_control=dual_control,
            approvals=approvals or [],
            scopes={"knowledge:entity-history:read"},
        ),
        required_scope="knowledge:entity-history:read",
    )


@mcp.tool()
def execute_entity_merge(
    namespace: str, preview: dict[str, Any], reviewer_id: str
) -> dict:
    """Atomically merge identities while preserving originals and undo history."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c).execute_merge(
            namespace,
            preview,
            reviewer_id=reviewer_id,
            principal_id=_context()[0],
            scopes={"knowledge:entity-history:execute"},
        ),
        write=True,
        required_scope="knowledge:entity-history:execute",
    )


@mcp.tool()
def preview_entity_split(
    namespace: str,
    source_id: str,
    new_entities: list[dict[str, Any]],
    reassignments: list[dict[str, Any]],
    ambiguous_object_ids: list[str] | None = None,
) -> dict:
    """Preview reviewed evidence reassignment and unresolved ambiguity."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c, initialize=False).split_preview(
            namespace,
            source_id,
            new_entities,
            reassignments,
            ambiguous_object_ids=ambiguous_object_ids or [],
            scopes={"knowledge:entity-history:read"},
        ),
        required_scope="knowledge:entity-history:read",
    )


@mcp.tool()
def execute_entity_split(
    namespace: str, preview: dict[str, Any], reviewer_id: str
) -> dict:
    """Atomically create split identities and reviewed object assignments."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c).execute_split(
            namespace,
            preview,
            reviewer_id=reviewer_id,
            principal_id=_context()[0],
            scopes={"knowledge:entity-history:execute"},
        ),
        write=True,
        required_scope="knowledge:entity-history:execute",
    )


@mcp.tool()
def undo_entity_identity_change(
    namespace: str, decision_id: str, reviewer_id: str
) -> dict:
    """Append an undo event and deactivate a merge or split projection."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c).undo(
            namespace,
            decision_id,
            reviewer_id=reviewer_id,
            principal_id=_context()[0],
            scopes={"knowledge:entity-history:execute"},
        ),
        write=True,
        required_scope="knowledge:entity-history:execute",
    )


@mcp.tool()
def register_entity_dependency(
    namespace: str,
    entity_id: str,
    dependent_type: str,
    dependent_id: str,
    independent: bool = False,
    payload: dict[str, Any] | None = None,
) -> dict:
    """Register graph, search, summary, watch, bundle, or metric dependency."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c).add_dependency(
            namespace,
            entity_id,
            dependent_type,
            dependent_id,
            independent=independent,
            payload=payload,
            principal_id=_context()[0],
            scopes={"knowledge:entity-history:write"},
        ),
        write=True,
        required_scope="knowledge:entity-history:write",
    )


@mcp.tool()
def inspect_entity_change_impact(
    namespace: str, entity_ids: list[str], limit: int = 500
) -> dict:
    """Inspect bounded affected and independent downstream objects."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c, initialize=False).impact(
            namespace, entity_ids, limit=limit, scopes={"knowledge:entity-history:read"}
        ),
        required_scope="knowledge:entity-history:read",
    )


@mcp.tool()
def publish_entity_change_rebuild(
    namespace: str,
    decision_id: str,
    generation: int,
    results: list[dict[str, Any]],
    cancel_requested: bool = False,
) -> dict:
    """Atomically publish a successful selective downstream rebuild."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c).publish_rebuild(
            namespace,
            decision_id,
            generation,
            results,
            cancel_requested=cancel_requested,
            principal_id=_context()[0],
            scopes={"knowledge:entity-history:execute"},
        ),
        write=True,
        required_scope="knowledge:entity-history:execute",
    )


@mcp.tool()
def get_entity_identity_history(
    namespace: str, entity_id: str, limit: int = 100, offset: int = 0
) -> dict:
    """List immutable, paginated identity decisions for an entity."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c, initialize=False).history(
            namespace,
            entity_id,
            limit=limit,
            offset=offset,
            scopes={"knowledge:entity-history:read"},
        ),
        required_scope="knowledge:entity-history:read",
    )


@mcp.tool()
def export_entity_identity_history(namespace: str, entity_ids: list[str]) -> dict:
    """Export entities and a dependency-complete immutable decision audit."""
    from src.kb.entity_history import EntityHistoryStore

    return _safe(
        lambda c: EntityHistoryStore(c, initialize=False).export(
            namespace, entity_ids, scopes={"knowledge:entity-history:read"}
        ),
        required_scope="knowledge:entity-history:read",
    )


@mcp.tool()
def record_language_text(
    namespace: str,
    object_type: str,
    object_id: str,
    original_text: str,
    language: str = "und",
    script: str = "Zyyy",
    locale: str | None = None,
    direction: str = "auto",
    code_switches: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    revision: int = 1,
) -> dict:
    """Record immutable original text plus declared/detected language metadata."""
    from src.kb.cross_language import CrossLanguageStore

    return _safe(
        lambda c: CrossLanguageStore(c).record_text(
            namespace,
            object_type,
            object_id,
            original_text,
            language=language,
            script=script,
            locale=locale,
            direction=direction,
            code_switches=code_switches or [],
            metadata=metadata,
            revision=revision,
            principal_id=_context()[0],
            scopes={"knowledge:cross-language:write"},
        ),
        write=True,
        required_scope="knowledge:cross-language:write",
    )


@mcp.tool()
def get_original_language_text(namespace: str, text_id: str) -> dict:
    """Retrieve source wording without substituting a translation."""
    from src.kb.cross_language import CrossLanguageStore

    return _safe(
        lambda c: CrossLanguageStore(c, initialize=False).get_text(
            namespace, text_id, scopes={"knowledge:cross-language:read"}
        ),
        required_scope="knowledge:cross-language:read",
    )


@mcp.tool()
def record_multilingual_alias(
    namespace: str,
    entity_id: str,
    alias_text: str,
    language: str,
    script: str,
    transliteration_system: str | None = None,
    confidence: float = 0.0,
    evidence: list[dict[str, Any]] | None = None,
    alternatives: list[str] | None = None,
    status: str = "candidate",
) -> dict:
    """Record a sourced multilingual alias or transliteration candidate."""
    from src.kb.cross_language import CrossLanguageStore

    return _safe(
        lambda c: CrossLanguageStore(c).record_alias(
            namespace,
            entity_id,
            alias_text,
            language,
            script,
            transliteration_system=transliteration_system,
            confidence=confidence,
            evidence=evidence or [],
            alternatives=alternatives or [],
            status=status,
            principal_id=_context()[0],
            scopes={"knowledge:cross-language:write"},
        ),
        write=True,
        required_scope="knowledge:cross-language:write",
    )


@mcp.tool()
def review_multilingual_alias(
    namespace: str,
    alias_id: str,
    decision: str,
    reviewer_id: str,
    rationale: str = "",
) -> dict:
    """Accept, reject, or preserve ambiguity for an alias decision."""
    from src.kb.cross_language import CrossLanguageStore

    return _safe(
        lambda c: CrossLanguageStore(c).review_alias(
            namespace,
            alias_id,
            decision,
            reviewer_id,
            rationale=rationale,
            principal_id=_context()[0],
            scopes={"knowledge:cross-language:review"},
        ),
        write=True,
        required_scope="knowledge:cross-language:review",
    )


@mcp.tool()
def align_cross_language_claims(
    namespace: str,
    source_claim_id: str,
    target_claim_id: str,
    relation: str,
    source_text_id: str,
    target_text_id: str,
    confidence: float = 0.0,
    evidence: list[dict[str, Any]] | None = None,
    analysis: dict[str, Any] | None = None,
    status: str = "candidate",
) -> dict:
    """Link translated, equivalent, narrower, broader, or divergent claims."""
    from src.kb.cross_language import CrossLanguageStore

    return _safe(
        lambda c: CrossLanguageStore(c).align_claims(
            namespace,
            source_claim_id,
            target_claim_id,
            relation,
            source_text_id,
            target_text_id,
            confidence=confidence,
            evidence=evidence or [],
            analysis=analysis,
            status=status,
            principal_id=_context()[0],
            scopes={"knowledge:cross-language:write"},
        ),
        write=True,
        required_scope="knowledge:cross-language:write",
    )


@mcp.tool()
def review_cross_language_alignment(
    namespace: str,
    alignment_id: str,
    decision: str,
    reviewer_id: str,
    rationale: str = "",
) -> dict:
    """Review a cross-language claim relation without erasing either claim."""
    from src.kb.cross_language import CrossLanguageStore

    return _safe(
        lambda c: CrossLanguageStore(c).review_alignment(
            namespace,
            alignment_id,
            decision,
            reviewer_id,
            rationale=rationale,
            principal_id=_context()[0],
            scopes={"knowledge:cross-language:review"},
        ),
        write=True,
        required_scope="knowledge:cross-language:review",
    )


@mcp.tool()
def compare_cross_language_claims(namespace: str, alignment_id: str) -> dict:
    """Inspect aligned source wording, relation, evidence, and divergence analysis."""
    from src.kb.cross_language import CrossLanguageStore

    return _safe(
        lambda c: CrossLanguageStore(c, initialize=False).compare_claims(
            namespace, alignment_id, scopes={"knowledge:cross-language:read"}
        ),
        required_scope="knowledge:cross-language:read",
    )


@mcp.tool()
def record_translation(
    namespace: str,
    source_text_id: str,
    target_language: str,
    translated_text: str,
    producer: dict[str, Any],
    version: int = 1,
    passage: dict[str, Any] | None = None,
    confidence: float = 0.0,
    alternatives: list[str] | None = None,
    status: str = "unreviewed",
) -> dict:
    """Record a versioned human, source-provided, or model translation."""
    from src.kb.cross_language import CrossLanguageStore

    return _safe(
        lambda c: CrossLanguageStore(c).record_translation(
            namespace,
            source_text_id,
            target_language,
            translated_text,
            producer,
            version=version,
            passage=passage,
            confidence=confidence,
            alternatives=alternatives or [],
            status=status,
            principal_id=_context()[0],
            scopes={"knowledge:cross-language:write"},
        ),
        write=True,
        required_scope="knowledge:cross-language:write",
    )


@mcp.tool()
def review_translation(
    namespace: str,
    translation_id: str,
    decision: str,
    reviewer_id: str,
    rationale: str = "",
) -> dict:
    """Append a human review or disagreement to translation history."""
    from src.kb.cross_language import CrossLanguageStore

    return _safe(
        lambda c: CrossLanguageStore(c).review_translation(
            namespace,
            translation_id,
            decision,
            reviewer_id,
            rationale=rationale,
            principal_id=_context()[0],
            scopes={"knowledge:cross-language:review"},
        ),
        write=True,
        required_scope="knowledge:cross-language:review",
    )


@mcp.tool()
def multilingual_search(
    namespace: str,
    query: str,
    languages: list[str] | None = None,
    include_translations: bool = True,
    limit: int = 20,
) -> dict:
    """Search originals, aliases, and translations with language-fair ranking."""
    from src.kb.cross_language import CrossLanguageStore

    return _safe(
        lambda c: CrossLanguageStore(c, initialize=False).search(
            namespace,
            query,
            languages=languages or [],
            include_translations=include_translations,
            limit=limit,
            scopes={"knowledge:cross-language:read"},
        ),
        required_scope="knowledge:cross-language:read",
    )


@mcp.tool()
def register_access_view_policy(
    namespace: str,
    policy_id: str,
    version: int,
    rules: dict[str, Any],
    status: str = "active",
) -> dict:
    """Register an immutable, versioned default-deny knowledge-view policy."""
    from src.kb.access_views import AccessViewStore

    return _safe(
        lambda c: AccessViewStore(c).register_policy(
            namespace,
            policy_id,
            version,
            rules,
            status=status,
            principal_id=_context()[0],
            scopes={"knowledge:views:admin"},
        ),
        write=True,
        required_scope="knowledge:views:admin",
    )


@mcp.tool()
def register_access_bound_object(
    namespace: str,
    object_type: str,
    object_id: str,
    classification: str,
    policy_id: str,
    policy_version: int,
    payload: dict[str, Any],
    source_license: str | None = None,
    jurisdiction: str | None = None,
    lineage: list[dict[str, Any]] | None = None,
    generation: int = 0,
    valid_time: dict[str, Any] | None = None,
    observed_at_ms: int | None = None,
) -> dict:
    """Bind an existing knowledge object to classification and view policy."""
    from src.kb.access_views import AccessViewStore

    return _safe(
        lambda c: AccessViewStore(c).register_object(
            namespace,
            object_type,
            object_id,
            classification,
            policy_id,
            policy_version,
            payload,
            source_license=source_license,
            jurisdiction=jurisdiction,
            lineage=lineage or [],
            generation=generation,
            valid_time=valid_time,
            observed_at_ms=observed_at_ms,
            principal_id=_context()[0],
            scopes={"knowledge:views:write"},
        ),
        write=True,
        required_scope="knowledge:views:write",
    )


@mcp.tool()
def inspect_effective_access_view(
    namespace: str,
    object_type: str,
    object_id: str,
    subject_principal_id: str,
    purpose: str,
    transformation: str = "read",
) -> dict:
    """Inspect an effective decision with administrator-only reason disclosure."""
    from src.kb.access_views import AccessViewStore

    return _safe(
        lambda c: AccessViewStore(c, initialize=False).decide(
            namespace,
            object_type,
            object_id,
            principal_id=subject_principal_id,
            purpose=purpose,
            transformation=transformation,
            scopes={"knowledge:views:admin"},
            disclose=True,
        ),
        required_scope="knowledge:views:admin",
    )


@mcp.tool()
def simulate_access_view(
    namespace: str,
    object_type: str,
    object_id: str,
    subject_principal_id: str,
    purpose: str,
    transformation: str = "read",
) -> dict:
    """Evaluate a hypothetical access request without changing state."""
    from src.kb.access_views import AccessViewStore

    return _safe(
        lambda c: AccessViewStore(c, initialize=False).simulate(
            namespace,
            object_type,
            object_id,
            principal_id=subject_principal_id,
            purpose=purpose,
            transformation=transformation,
            scopes={"knowledge:views:admin"},
        ),
        required_scope="knowledge:views:admin",
    )


@mcp.tool()
def filter_access_bound_query(
    namespace: str,
    candidates: list[dict[str, Any]],
    purpose: str,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Filter candidates before ranking, aggregation, counts, or pagination."""
    from src.kb.access_views import AccessViewStore

    return _safe(
        lambda c: AccessViewStore(c, initialize=False).filter_query(
            namespace,
            candidates,
            principal_id=_context()[0],
            purpose=purpose,
            scopes={"knowledge:views:read"},
            limit=limit,
            offset=offset,
        ),
        write=True,
        required_scope="knowledge:views:read",
    )


@mcp.tool()
def derive_redacted_projection(
    namespace: str,
    object_type: str,
    object_id: str,
    transformation: str,
    redacted_payload: dict[str, Any],
    purpose: str,
    generation: int = 0,
) -> dict:
    """Create a policy-versioned projection with opaque safe lineage."""
    from src.kb.access_views import AccessViewStore

    return _safe(
        lambda c: AccessViewStore(c).derive_redacted(
            namespace,
            object_type,
            object_id,
            transformation,
            redacted_payload,
            principal_id=_context()[0],
            purpose=purpose,
            generation=generation,
            scopes={"knowledge:views:write"},
        ),
        write=True,
        required_scope="knowledge:views:write",
    )


@mcp.tool()
def create_access_share_grant(
    namespace: str,
    recipient_id: str,
    purpose: str,
    expires_at_ms: int,
    policy_id: str,
    policy_version: int,
    object_ids: list[str],
    redistribution: bool = False,
    watermark_required: bool = True,
) -> dict:
    """Create a recipient-, purpose-, and expiry-bound export grant."""
    from src.kb.access_views import AccessViewStore

    return _safe(
        lambda c: AccessViewStore(c).create_grant(
            namespace,
            recipient_id,
            purpose,
            expires_at_ms,
            policy_id,
            policy_version,
            object_ids,
            redistribution=redistribution,
            watermark_required=watermark_required,
            principal_id=_context()[0],
            scopes={"knowledge:views:export"},
        ),
        write=True,
        required_scope="knowledge:views:export",
    )


@mcp.tool()
def authorize_access_export(
    namespace: str,
    grant_id: str,
    recipient_id: str,
    purpose: str,
    object_ids: list[str],
    watermark: str | None = None,
    redistribution: bool = False,
) -> dict:
    """Authorize an export without revealing ungranted object details."""
    from src.kb.access_views import AccessViewStore

    return _safe(
        lambda c: AccessViewStore(c).authorize_export(
            namespace,
            grant_id,
            recipient_id,
            purpose,
            object_ids,
            watermark=watermark,
            redistribution=redistribution,
            principal_id=_context()[0],
            scopes={"knowledge:views:export"},
        ),
        write=True,
        required_scope="knowledge:views:export",
    )


@mcp.tool()
def revoke_access_share_grant(namespace: str, grant_id: str) -> dict:
    """Revoke a grant so downstream export authorization immediately fails."""
    from src.kb.access_views import AccessViewStore

    return _safe(
        lambda c: AccessViewStore(c).revoke_grant(
            namespace,
            grant_id,
            principal_id=_context()[0],
            scopes={"knowledge:views:export"},
        ),
        write=True,
        required_scope="knowledge:views:export",
    )


@mcp.tool()
def get_access_view_audit(namespace: str, limit: int = 100) -> dict:
    """Read bounded access decisions under separate administrator authority."""
    from src.kb.access_views import AccessViewStore

    return _safe(
        lambda c: AccessViewStore(c, initialize=False).audit(
            namespace, limit=limit, scopes={"knowledge:views:admin"}
        ),
        required_scope="knowledge:views:admin",
    )


@mcp.tool()
def inspect_access_view_health(namespace: str) -> dict:
    """Report invalidated projections and expired active grants."""
    from src.kb.access_views import AccessViewStore

    return _safe(
        lambda c: AccessViewStore(c, initialize=False).health(
            namespace, scopes={"knowledge:views:admin"}
        ),
        required_scope="knowledge:views:admin",
    )


@mcp.tool()
def register_anomaly_watch(
    namespace: str,
    watch_key: str,
    version: int,
    signal_type: str,
    scope: dict[str, Any],
    baseline: dict[str, Any],
    detector: dict[str, Any],
    notification: dict[str, Any],
    status: str = "active",
) -> dict:
    """Register a versioned metric, event, graph, source, coverage, or narrative watch."""
    from src.kb.knowledge_anomalies import KnowledgeAnomalyStore

    return _safe(
        lambda c: KnowledgeAnomalyStore(c).register_watch(
            namespace,
            watch_key,
            version,
            signal_type,
            scope,
            baseline,
            detector,
            notification,
            status=status,
            principal_id=_context()[0],
            scopes={"knowledge:anomalies:write"},
        ),
        write=True,
        required_scope="knowledge:anomalies:write",
    )


@mcp.tool()
def preview_anomaly_baseline(
    namespace: str, watch_id: str, observations: list[dict[str, Any]]
) -> dict:
    """Preview a bounded baseline, missingness, sparsity, and seasonality."""
    from src.kb.knowledge_anomalies import KnowledgeAnomalyStore

    return _safe(
        lambda c: KnowledgeAnomalyStore(c, initialize=False).preview_baseline(
            namespace, watch_id, observations, scopes={"knowledge:anomalies:read"}
        ),
        required_scope="knowledge:anomalies:read",
    )


@mcp.tool()
def simulate_anomaly_detector(
    namespace: str, watch_id: str, observations: list[dict[str, Any]], limit: int = 1000
) -> dict:
    """Simulate a detector without persisting a run or anomaly."""
    from src.kb.knowledge_anomalies import KnowledgeAnomalyStore

    return _safe(
        lambda c: KnowledgeAnomalyStore(c, initialize=False).simulate(
            namespace,
            watch_id,
            observations,
            limit=limit,
            scopes={"knowledge:anomalies:read"},
        ),
        required_scope="knowledge:anomalies:read",
    )


@mcp.tool()
def run_anomaly_detector(
    namespace: str,
    watch_id: str,
    observations: list[dict[str, Any]],
    generation: int,
    cancel_requested: bool = False,
    limit: int = 1000,
) -> dict:
    """Run a bounded, cancellable, deterministic anomaly detector."""
    from src.kb.knowledge_anomalies import KnowledgeAnomalyStore

    return _safe(
        lambda c: KnowledgeAnomalyStore(c).run(
            namespace,
            watch_id,
            observations,
            generation,
            cancel_requested=cancel_requested,
            limit=limit,
            principal_id=_context()[0],
            scopes={"knowledge:anomalies:execute"},
        ),
        write=True,
        required_scope="knowledge:anomalies:execute",
    )


@mcp.tool()
def get_knowledge_anomaly(namespace: str, anomaly_id: str) -> dict:
    """Inspect an anomaly score, baseline, evidence context, and explanations."""
    from src.kb.knowledge_anomalies import KnowledgeAnomalyStore

    return _safe(
        lambda c: KnowledgeAnomalyStore(c, initialize=False).anomaly(
            namespace, anomaly_id, scopes={"knowledge:anomalies:read"}
        ),
        required_scope="knowledge:anomalies:read",
    )


@mcp.tool()
def correlate_knowledge_anomaly(
    namespace: str, anomaly_id: str, candidates: list[dict[str, Any]], limit: int = 100
) -> dict:
    """Attach ranked plausible—not proven—knowledge and source changes."""
    from src.kb.knowledge_anomalies import KnowledgeAnomalyStore

    return _safe(
        lambda c: KnowledgeAnomalyStore(c).correlate(
            namespace,
            anomaly_id,
            candidates,
            limit=limit,
            principal_id=_context()[0],
            scopes={"knowledge:anomalies:write"},
        ),
        write=True,
        required_scope="knowledge:anomalies:write",
    )


@mcp.tool()
def deliver_anomaly_alert(
    namespace: str,
    anomaly_id: str,
    subscriber_id: str,
    delivery_outcome: str = "delivered",
    cancel_requested: bool = False,
) -> dict:
    """Deliver, suppress, deduplicate, retry, or cancel an anomaly alert."""
    from src.kb.knowledge_anomalies import KnowledgeAnomalyStore

    return _safe(
        lambda c: KnowledgeAnomalyStore(c).deliver(
            namespace,
            anomaly_id,
            subscriber_id,
            delivery_outcome=delivery_outcome,
            cancel_requested=cancel_requested,
            principal_id=_context()[0],
            scopes={"knowledge:anomalies:deliver"},
        ),
        write=True,
        required_scope="knowledge:anomalies:deliver",
    )


@mcp.tool()
def transition_anomaly_alert(
    namespace: str, alert_id: str, action: str, actor_id: str
) -> dict:
    """Acknowledge, resolve, or reopen an alert with append-only history."""
    from src.kb.knowledge_anomalies import KnowledgeAnomalyStore

    return _safe(
        lambda c: KnowledgeAnomalyStore(c).transition_alert(
            namespace,
            alert_id,
            action,
            actor_id,
            principal_id=_context()[0],
            scopes={"knowledge:anomalies:deliver"},
        ),
        write=True,
        required_scope="knowledge:anomalies:deliver",
    )


@mcp.tool()
def anomaly_alert_history(namespace: str, limit: int = 100, offset: int = 0) -> dict:
    """List bounded, paginated alert delivery and recovery history."""
    from src.kb.knowledge_anomalies import KnowledgeAnomalyStore

    return _safe(
        lambda c: KnowledgeAnomalyStore(c, initialize=False).history(
            namespace, limit=limit, offset=offset, scopes={"knowledge:anomalies:read"}
        ),
        required_scope="knowledge:anomalies:read",
    )


@mcp.tool()
def inspect_anomaly_health(namespace: str) -> dict:
    """Report active watches, open anomalies, and retrying alerts."""
    from src.kb.knowledge_anomalies import KnowledgeAnomalyStore

    return _safe(
        lambda c: KnowledgeAnomalyStore(c, initialize=False).health(
            namespace, scopes={"knowledge:anomalies:read"}
        ),
        required_scope="knowledge:anomalies:read",
    )


@mcp.tool()
def register_retention_policy(
    namespace: str,
    policy_id: str,
    version: int,
    rules: dict[str, Any],
    parent_policy_id: str | None = None,
    status: str = "active",
) -> dict:
    """Register an immutable retention policy with optional inheritance."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c).register_policy(
            namespace,
            policy_id,
            version,
            rules,
            parent_policy_id=parent_policy_id,
            status=status,
            principal_id=_context()[0],
            scopes={"knowledge:retention:admin"},
        ),
        write=True,
        required_scope="knowledge:retention:admin",
    )


@mcp.tool()
def register_retention_object(
    namespace: str,
    object_id: str,
    object_class: str,
    policy_id: str,
    policy_version: int,
    payload: dict[str, Any],
    created_at_ms: int,
    source_license: str | None = None,
    access_class: str = "public",
    value_score: float = 0.0,
    generation: int = 0,
    dependencies: list[str] | None = None,
    pins: list[str] | None = None,
) -> dict:
    """Attach retention metadata to an existing knowledge object identity."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c).register_object(
            namespace,
            object_id,
            object_class,
            policy_id,
            policy_version,
            payload,
            created_at_ms=created_at_ms,
            source_license=source_license,
            access_class=access_class,
            value_score=value_score,
            generation=generation,
            dependencies=dependencies or [],
            pins=pins or [],
            principal_id=_context()[0],
            scopes={"knowledge:retention:admin"},
        ),
        write=True,
        required_scope="knowledge:retention:admin",
    )


@mcp.tool()
def place_retention_legal_hold(
    namespace: str, object_id: str, reason: str, expires_at_ms: int | None = None
) -> dict:
    """Place a finite or indefinite legal hold on a knowledge object."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c).place_hold(
            namespace,
            object_id,
            reason,
            expires_at_ms=expires_at_ms,
            principal_id=_context()[0],
            scopes={"knowledge:retention:admin"},
        ),
        write=True,
        required_scope="knowledge:retention:admin",
    )


@mcp.tool()
def release_retention_legal_hold(namespace: str, hold_id: str) -> dict:
    """Release a legal hold while preserving its audit record."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c).release_hold(
            namespace,
            hold_id,
            principal_id=_context()[0],
            scopes={"knowledge:retention:admin"},
        ),
        write=True,
        required_scope="knowledge:retention:admin",
    )


@mcp.tool()
def simulate_retention_eligibility(namespace: str, object_id: str) -> dict:
    """Explain a side-effect-free retention or deletion decision."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c, initialize=False).explain(
            namespace, object_id, scopes={"knowledge:retention:read"}
        ),
        required_scope="knowledge:retention:read",
    )


@mcp.tool()
def create_retention_checkpoint(
    namespace: str,
    generation_start: int,
    generation_end: int,
    records: list[dict[str, Any]],
    schema_version: str,
    tombstones: list[str] | None = None,
    cancel_requested: bool = False,
    limit: int = 1000,
) -> dict:
    """Compact immutable history into a bounded content-addressed checkpoint."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c).checkpoint(
            namespace,
            generation_start,
            generation_end,
            records,
            schema_version=schema_version,
            tombstones=tombstones or [],
            cancel_requested=cancel_requested,
            limit=limit,
            principal_id=_context()[0],
            scopes={"knowledge:retention:execute"},
        ),
        write=True,
        required_scope="knowledge:retention:execute",
    )


@mcp.tool()
def verify_retention_checkpoint(
    namespace: str,
    checkpoint_id: str,
    records: list[dict[str, Any]] | None = None,
    tombstones: list[str] | None = None,
) -> dict:
    """Verify checkpoint identity and optionally compare supplied content."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c, initialize=False).verify_checkpoint(
            namespace,
            checkpoint_id,
            records=records,
            tombstones=tombstones,
            scopes={"knowledge:retention:read"},
        ),
        required_scope="knowledge:retention:read",
    )


@mcp.tool()
def archive_knowledge_checkpoint(
    namespace: str,
    checkpoint_id: str,
    storage: dict[str, Any],
    encryption: dict[str, Any] | None = None,
    storage_available: bool = True,
    partial: bool = False,
    cancel_requested: bool = False,
) -> dict:
    """Archive a checkpoint through a pluggable storage manifest."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c).archive(
            namespace,
            checkpoint_id,
            storage,
            encryption=encryption,
            storage_available=storage_available,
            partial=partial,
            cancel_requested=cancel_requested,
            principal_id=_context()[0],
            scopes={"knowledge:retention:execute"},
        ),
        write=True,
        required_scope="knowledge:retention:execute",
    )


@mcp.tool()
def restore_knowledge_archive(
    namespace: str, archive_id: str, storage_available: bool = True,
    manifest: dict[str, Any] | None = None,
    supported_schema_versions: list[str] | None = None,
) -> dict:
    """Verify and atomically restore a cold archive."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c).restore(
            namespace,
            archive_id,
            storage_available=storage_available,
            manifest=manifest,
            supported_schema_versions=tuple(supported_schema_versions or ["1", "2", "3"]),
            principal_id=_context()[0],
            scopes={"knowledge:retention:execute"},
        ),
        write=True,
        required_scope="knowledge:retention:execute",
    )


@mcp.tool()
def plan_retention_gc(namespace: str, object_ids: list[str], limit: int = 1000) -> dict:
    """Dry-run dependency, snapshot, bundle, export, session, and hold checks."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c).plan_gc(
            namespace,
            object_ids,
            limit=limit,
            principal_id=_context()[0],
            scopes={"knowledge:retention:admin"},
        ),
        write=True,
        required_scope="knowledge:retention:admin",
    )


@mcp.tool()
def execute_retention_gc(
    namespace: str,
    plan: dict[str, Any],
    cancel_requested: bool = False,
    deletion_outcome: str = "success",
) -> dict:
    """Recheck plan guards and atomically tombstone dependency-unreachable objects."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c).execute_gc(
            namespace,
            plan,
            cancel_requested=cancel_requested,
            deletion_outcome=deletion_outcome,
            principal_id=_context()[0],
            scopes={"knowledge:retention:execute"},
        ),
        write=True,
        required_scope="knowledge:retention:execute",
    )


@mcp.tool()
def get_retention_job(namespace: str, job_id: str) -> dict:
    """Inspect a retention job receipt and terminal status."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c, initialize=False).job(
            namespace, job_id, scopes={"knowledge:retention:read"}
        ),
        required_scope="knowledge:retention:read",
    )


@mcp.tool()
def cancel_retention_job(namespace: str, job_id: str) -> dict:
    """Cancel a non-terminal retention job."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c).cancel_job(
            namespace,
            job_id,
            principal_id=_context()[0],
            scopes={"knowledge:retention:execute"},
        ),
        write=True,
        required_scope="knowledge:retention:execute",
    )


@mcp.tool()
def inspect_retention_health(namespace: str) -> dict:
    """Report capacity-facing retention, archive, hold, and failure counts."""
    from src.kb.knowledge_retention import KnowledgeRetentionStore

    return _safe(
        lambda c: KnowledgeRetentionStore(c, initialize=False).health(
            namespace, scopes={"knowledge:retention:read"}
        ),
        required_scope="knowledge:retention:read",
    )


@mcp.tool()
def create_research_project(namespace: str, request_key: str, questions: list[str],
                            success_criteria: list[str], scope: dict[str, Any], budget: dict[str, int]) -> dict:
    """Create an owner-scoped persistent investigation with explicit success criteria and budget."""
    from src.kb.research_projects import ResearchProjectStore, WRITE_SCOPE
    return _safe(lambda c: ResearchProjectStore(c).create(
        namespace, request_key, questions=questions, success_criteria=success_criteria,
        scope=scope, budget=budget, principal_id=_context()[0], scopes=_context()[1]),
        write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def create_authored_report(namespace: str, request_key: str, content: dict[str, Any]) -> dict:
    """Persist authored sections, assertions, source revisions, bibliography, and limitations."""
    from src.kb.authored_reports import AuthoredReportStore, WRITE_SCOPE
    return _safe(lambda c: AuthoredReportStore(c).create(namespace, request_key, content,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def create_binary_forecast(namespace: str, request_key: str, question: str, outcome_rule: str,
                            resolution_at_ms: int, probability: float, evidence: list[dict[str, Any]],
                            resolution_match: dict[str, str] | None = None) -> dict:
    """Record an explicit probability, resolution rule, deadline, forecaster, and evidence references."""
    from src.kb.forecasts import ForecastStore, WRITE_SCOPE
    return _safe(lambda c: ForecastStore(c).create(namespace, request_key, question=question,
        outcome_rule=outcome_rule, resolution_at_ms=resolution_at_ms, probability=probability, evidence=evidence,
        resolution_match=resolution_match, principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def create_research_decision(namespace: str, request_key: str, content: dict[str, Any]) -> dict:
    """Record options, preferences, evidence, action, and review conditions against a project revision."""
    from src.kb.decisions import DecisionStore, WRITE_SCOPE
    return _safe(lambda c: DecisionStore(c).create(namespace, request_key, content,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def create_review_protocol(namespace: str, request_key: str, content: dict[str, Any]) -> dict:
    """Register eligibility criteria, search plan, independent reviewers, and study fields."""
    from src.kb.systematic_reviews import SystematicReviewStore, WRITE_SCOPE
    return _safe(lambda c: SystematicReviewStore(c).create(namespace, request_key, content,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def sync_zotero_library(namespace: str, library_id: str, library_type: str, mode: str = "web",
                        credential_env: str | None = None, max_items: int = 10000, timeout_seconds: int = 60) -> dict:
    """Read Zotero v3 into owner-scoped history with an atomic incremental checkpoint; no write-back."""
    from src.ingestion.zotero_sync import ZoteroSyncStore, ZoteroReadClient, ZoteroSyncError, WRITE_SCOPE, _authorize
    def sync(conn):
        import os
        import re
        principal, scopes = _context()
        _authorize(namespace, principal, scopes, write=True)
        key = None
        if credential_env:
            if mode != "web" or not re.fullmatch(r"NOESIS_ZOTERO_[A-Z0-9_]+", credential_env):
                raise ZoteroSyncError("invalid_credentials", "Web credentials require a configured NOESIS_ZOTERO_ environment reference")
            if "operator" not in scopes and f"credential:{credential_env}:use" not in scopes:
                raise ZoteroSyncError("unauthorized", "current permission to use this credential is required")
            key = os.getenv(credential_env)
            if not key:
                raise ZoteroSyncError("credentials_unavailable", "configured Zotero credential is unavailable")
        client = ZoteroReadClient(library_id, library_type, mode=mode, api_key=key, timeout_seconds=timeout_seconds)
        try:
            return ZoteroSyncStore(conn).sync(namespace, client, principal_id=principal, scopes=scopes, max_items=max_items)
        finally:
            client.close()
    return _safe(sync, write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def list_zotero_items(namespace: str, library: str, include_deleted: bool = False, limit: int = 100, offset: int = 0) -> dict:
    """List the current owner's imported items, including explicit attachment and deletion states."""
    from src.ingestion.zotero_sync import ZoteroSyncStore, READ_SCOPE
    return _safe(lambda c: ZoteroSyncStore(c, initialize=False).items(namespace, library, include_deleted=include_deleted,
        limit=limit, offset=offset, principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def inspect_zotero_item(namespace: str, library: str, key: str, version: int | None = None) -> dict:
    """Reopen an imported item revision while separately reporting its current external lifecycle."""
    from src.ingestion.zotero_sync import ZoteroSyncStore, READ_SCOPE
    return _safe(lambda c: ZoteroSyncStore(c, initialize=False).inspect_item(namespace, library, key, version=version,
        principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def export_zotero_bibliography(namespace: str, library: str, item_keys: list[str], item_versions: dict[str, int] | None = None,
                                report_id: str | None = None, report_namespace: str | None = None) -> dict:
    """Export stable-key CSL JSON/BibTeX with pinned item versions and optional report citation closure."""
    from src.ingestion.zotero_sync import ZoteroSyncStore, READ_SCOPE
    return _safe(lambda c: ZoteroSyncStore(c, initialize=False).export_bibliography(namespace, library, item_keys,
        item_versions=item_versions, report_id=report_id, report_namespace=report_namespace,
        principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def inspect_review_protocol(namespace: str, protocol_id: str, revision: int | None = None) -> dict:
    """Inspect current or historical protocol criteria under current access."""
    from src.kb.systematic_reviews import SystematicReviewStore, READ_SCOPE
    return _safe(lambda c: SystematicReviewStore(c, initialize=False).inspect(namespace, protocol_id, revision=revision,
        principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def amend_review_protocol(namespace: str, protocol_id: str, expected_revision: int, content: dict[str, Any], rationale: str) -> dict:
    """Append a visible protocol amendment without rewriting candidates' original criteria."""
    from src.kb.systematic_reviews import SystematicReviewStore, WRITE_SCOPE
    return _safe(lambda c: SystematicReviewStore(c).amend(namespace, protocol_id, expected_revision, content, rationale,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def add_review_candidate(namespace: str, protocol_id: str, protocol_revision: int, publication_id: str,
                          source_revision: str, source_namespace: str, search_run_id: str, study_id: str,
                          title: str, abstract: str, full_text_available: bool) -> dict:
    """Trace a publication to its search run, protocol version, source revision, and study group."""
    from src.kb.systematic_reviews import SystematicReviewStore, WRITE_SCOPE
    return _safe(lambda c: SystematicReviewStore(c).add_candidate(namespace, protocol_id, protocol_revision,
        publication_id=publication_id, source_revision=source_revision, source_namespace=source_namespace,
        search_run_id=search_run_id, study_id=study_id, title=title, abstract=abstract, full_text_available=full_text_available,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def screen_review_candidate(namespace: str, candidate_id: str, stage: str, expected_revision: int, decision: str, reason: str) -> dict:
    """Record the current reviewer's independent eligibility decision and reason."""
    from src.kb.systematic_reviews import SystematicReviewStore, WRITE_SCOPE
    return _safe(lambda c: SystematicReviewStore(c).screen(namespace, candidate_id, stage, expected_revision, decision, reason,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def list_review_candidates(namespace: str, protocol_id: str, limit: int = 50, offset: int = 0) -> dict:
    """List authorized screening candidates while hiding other reviewers' decisions."""
    from src.kb.systematic_reviews import SystematicReviewStore, READ_SCOPE
    return _safe(lambda c: SystematicReviewStore(c, initialize=False).list_candidates(namespace, protocol_id,
        limit=limit, offset=offset, principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def inspect_review_candidate(namespace: str, candidate_id: str) -> dict:
    """Inspect a candidate with independent reviewers blinded to one another's decisions."""
    from src.kb.systematic_reviews import SystematicReviewStore, READ_SCOPE
    return _safe(lambda c: SystematicReviewStore(c, initialize=False).inspect_candidate(namespace, candidate_id,
        principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def adjudicate_review_candidate(namespace: str, candidate_id: str, stage: str, screening_hash: str, decision: str, reason: str) -> dict:
    """Resolve a screening disagreement against its exact reviewed decision set."""
    from src.kb.systematic_reviews import SystematicReviewStore, WRITE_SCOPE
    return _safe(lambda c: SystematicReviewStore(c).adjudicate(namespace, candidate_id, stage, screening_hash, decision, reason,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def extract_review_field(namespace: str, candidate_id: str, field_name: str, value: str, start: int, end: int) -> dict:
    """Propose a protocol-defined study value anchored to an exact committed source span."""
    from src.kb.systematic_reviews import SystematicReviewStore, WRITE_SCOPE
    return _safe(lambda c: SystematicReviewStore(c).extract_field(namespace, candidate_id, field_name, value, start, end,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def review_study_field(namespace: str, field_id: str, expected_revision: int, decision: str, reason: str) -> dict:
    """Record a second reviewer's evaluation of a proposed study-field value."""
    from src.kb.systematic_reviews import SystematicReviewStore, WRITE_SCOPE
    return _safe(lambda c: SystematicReviewStore(c).review_field(namespace, field_id, expected_revision, decision, reason,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def export_systematic_review(namespace: str, protocol_id: str, limit: int = 10000) -> dict:
    """Export screening counts, study fields, unresolved cases, amendments, and ASReview input."""
    from src.kb.systematic_reviews import SystematicReviewStore, READ_SCOPE
    return _safe(lambda c: SystematicReviewStore(c, initialize=False).export(namespace, protocol_id, limit=limit,
        principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def inspect_research_decision(namespace: str, decision_id: str, revision: int | None = None) -> dict:
    """Inspect current or historical decision context under current project access."""
    from src.kb.decisions import DecisionStore, READ_SCOPE
    return _safe(lambda c: DecisionStore(c, initialize=False).inspect(namespace, decision_id, revision=revision,
        principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def revise_research_decision(namespace: str, decision_id: str, expected_revision: int, content: dict[str, Any]) -> dict:
    """Append a new decision while preserving earlier choices and their project baselines."""
    from src.kb.decisions import DecisionStore, WRITE_SCOPE
    return _safe(lambda c: DecisionStore(c).revise(namespace, decision_id, expected_revision, content,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def calculate_decision_sensitivity(namespace: str, decision_id: str, revision: int, weights: dict[str, Any],
                                    inputs: dict[str, Any], scenarios: list[dict[str, Any]], provenance: str) -> dict:
    """Record bounded weighted-utility comparisons with ties, missing data, and formula provenance."""
    from src.kb.decisions import DecisionStore, WRITE_SCOPE
    return _safe(lambda c: DecisionStore(c).sensitivity(namespace, decision_id, revision, weights=weights,
        inputs=inputs, scenarios=scenarios, provenance=provenance, principal_id=_context()[0], scopes=_context()[1]),
        write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def inspect_binary_forecast(namespace: str, forecast_id: str, cutoff_ms: int | None = None,
                            outcome_cutoff_ms: int | None = None) -> dict:
    """Inspect a forecast and reviewed outcome as recorded by explicit historical cutoffs."""
    from src.kb.forecasts import ForecastStore, READ_SCOPE
    return _safe(lambda c: ForecastStore(c, initialize=False).inspect(namespace, forecast_id,
        cutoff_ms=cutoff_ms, outcome_cutoff_ms=outcome_cutoff_ms, principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def revise_binary_forecast(namespace: str, forecast_id: str, expected_revision: int, probability: float,
                            evidence: list[dict[str, Any]], rationale: str, outcome_rule: str | None = None,
                            resolution_match: dict[str, str] | None = None) -> dict:
    """Revise a forecast before its deadline, preserving prior probabilities and rule versions."""
    from src.kb.forecasts import ForecastStore, WRITE_SCOPE
    return _safe(lambda c: ForecastStore(c).revise(namespace, forecast_id, expected_revision,
        probability=probability, evidence=evidence, rationale=rationale, outcome_rule=outcome_rule,
        resolution_match=resolution_match, principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def propose_forecast_resolution(namespace: str, forecast_id: str) -> dict:
    """Match registered quantitative rules to sourced observations without settling the forecast."""
    from src.kb.forecasts import ForecastStore, READ_SCOPE
    return _safe(lambda c: ForecastStore(c, initialize=False).propose_resolution(namespace, forecast_id,
        principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def resolve_binary_forecast(namespace: str, forecast_id: str, expected_outcome_revision: int, status: str,
                            outcome: int | None, evidence: list[dict[str, Any]], rationale: str,
                            forecast_revision: int) -> dict:
    """Record a reviewed outcome, dispute, cancellation, or retrospective correction."""
    from src.kb.forecasts import ForecastStore, WRITE_SCOPE
    return _safe(lambda c: ForecastStore(c).resolve(namespace, forecast_id, expected_outcome_revision,
        status=status, outcome=outcome, evidence=evidence, rationale=rationale, forecast_revision=forecast_revision,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def score_binary_forecasts(namespace: str, forecast_ids: list[str], cutoff_ms: int,
                            outcome_cutoff_ms: int | None = None) -> dict:
    """Score a specified cohort with Brier scores, reliability intervals, baseline, and exclusions."""
    from src.kb.forecasts import ForecastStore, READ_SCOPE
    return _safe(lambda c: ForecastStore(c, initialize=False).score(namespace, forecast_ids, cutoff_ms=cutoff_ms,
        outcome_cutoff_ms=outcome_cutoff_ms, principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def inspect_authored_report(namespace: str, report_id: str, revision: int | None = None) -> dict:
    """Reopen the current or historical report without regenerating authored wording."""
    from src.kb.authored_reports import AuthoredReportStore, READ_SCOPE
    return _safe(lambda c: AuthoredReportStore(c, initialize=False).inspect(namespace, report_id,
        revision=revision, principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def revise_authored_report(namespace: str, report_id: str, expected_revision: int, content: dict[str, Any]) -> dict:
    """Append an authored revision with conflict checks and preserved historical versions."""
    from src.kb.authored_reports import AuthoredReportStore, WRITE_SCOPE
    return _safe(lambda c: AuthoredReportStore(c).revise(namespace, report_id, expected_revision, content,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def export_authored_report(namespace: str, report_id: str, revision: int | None = None) -> dict:
    """Export report JSON, Markdown, and bibliography without external publication."""
    from src.kb.authored_reports import AuthoredReportStore, READ_SCOPE
    return _safe(lambda c: AuthoredReportStore(c, initialize=False).export(namespace, report_id,
        revision=revision, principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def reopen_authored_report(namespace: str, request_key: str, package: dict[str, Any]) -> dict:
    """Verify an exported report's content hash and preserve its authored contents in a new ledger."""
    from src.kb.authored_reports import AuthoredReportStore, WRITE_SCOPE
    return _safe(lambda c: AuthoredReportStore(c).reopen(namespace, request_key, package,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def inspect_research_project(namespace: str, project_id: str, revision: int | None = None) -> dict:
    """Reopen a project or historical revision without rerunning its completed work."""
    from src.kb.research_projects import ResearchProjectStore, READ_SCOPE
    return _safe(lambda c: ResearchProjectStore(c, initialize=False).inspect(
        namespace, project_id, revision=revision, principal_id=_context()[0], scopes=_context()[1]),
        required_scope=READ_SCOPE)


@mcp.tool()
def list_research_projects(namespace: str, limit: int = 50, offset: int = 0) -> dict:
    """List currently authorized projects in a namespace."""
    from src.kb.research_projects import ResearchProjectStore, READ_SCOPE
    return _safe(lambda c: ResearchProjectStore(c, initialize=False).list(
        namespace, limit=limit, offset=offset, principal_id=_context()[0], scopes=_context()[1]),
        required_scope=READ_SCOPE)


@mcp.tool()
def revise_research_project(namespace: str, project_id: str, expected_revision: int,
                            questions: list[str] | None = None, success_criteria: list[str] | None = None,
                            add_links: list[dict[str, Any]] | None = None, status: str | None = None,
                            replace_links: list[dict[str, Any]] | None = None) -> dict:
    """Append question, evidence, or lifecycle changes with optimistic revision checks."""
    from src.kb.research_projects import ResearchProjectStore, WRITE_SCOPE
    return _safe(lambda c: ResearchProjectStore(c).revise(
        namespace, project_id, expected_revision, questions=questions, success_criteria=success_criteria,
        add_links=add_links, replace_links=replace_links, status=status, principal_id=_context()[0], scopes=_context()[1]),
        write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def branch_research_project(namespace: str, project_id: str, revision: int, request_key: str,
                            baseline: dict[str, int], changes: dict[str, Any], budget: dict[str, int]) -> dict:
    """Branch an explicit project revision against retained committed namespace generations."""
    from src.kb.project_branches import ProjectBranchStore
    from src.kb.research_projects import WRITE_SCOPE
    return _safe(lambda c: ProjectBranchStore(c).branch(namespace, project_id, revision, request_key,
        baseline=baseline, changes=changes, budget=budget, principal_id=_context()[0], scopes=_context()[1]),
        write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def compare_research_projects(namespace: str, left_id: str, right_id: str) -> dict:
    """Compare shared-baseline references and costs, disclosing unavailable evidence and coverage."""
    from src.kb.project_branches import ProjectBranchStore
    from src.kb.research_projects import READ_SCOPE
    return _safe(lambda c: ProjectBranchStore(c, initialize=False).compare(namespace, left_id, right_id,
        principal_id=_context()[0], scopes=_context()[1]), required_scope=READ_SCOPE)


@mcp.tool()
def archive_research_project(namespace: str, project_id: str, expected_revision: int) -> dict:
    """Archive a project while retaining its question and evidence history."""
    from src.kb.research_projects import ResearchProjectStore, WRITE_SCOPE
    return _safe(lambda c: ResearchProjectStore(c).revise(
        namespace, project_id, expected_revision, status="archived", principal_id=_context()[0], scopes=_context()[1]),
        write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def record_research_project_expenditure(namespace: str, project_id: str, receipt_id: str,
                                        costs: dict[str, int], expected_revision: int) -> dict:
    """Account for a committed execution receipt once against a project's cumulative budget."""
    from src.kb.research_projects import ResearchProjectStore, WRITE_SCOPE
    return _safe(lambda c: ResearchProjectStore(c).record_expenditure(
        namespace, project_id, receipt_id, costs, expected_revision, principal_id=_context()[0], scopes=_context()[1]),
        write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def validate_research_package_manifest(
    manifest: dict[str, Any], supported_versions: list[str] | None = None
) -> dict:
    """Validate extensions, required fields, and format-version compatibility."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c, initialize=False).validate_manifest(
            manifest, supported_versions=tuple(supported_versions or ["1.0"])
        ),
        required_scope="knowledge:packages:read",
    )


@mcp.tool()
def create_research_package_manifest(
    namespace: str,
    manifest: dict[str, Any],
    supported_versions: list[str] | None = None,
) -> dict:
    """Create a canonical, immutable portable research-package manifest."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c).create_manifest(
            namespace,
            manifest,
            supported_versions=tuple(supported_versions or ["1.0"]),
            principal_id=_context()[0],
            scopes={"knowledge:packages:write"},
        ),
        write=True,
        required_scope="knowledge:packages:write",
    )


@mcp.tool()
def register_research_package_component(
    namespace: str,
    component_type: str,
    component_id: str,
    content: dict[str, Any],
    dependencies: list[str] | None = None,
    access_status: str = "accessible",
    redacted_content: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Register a content-addressed package member and dependency edges."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c).register_component(
            namespace,
            component_type,
            component_id,
            content,
            dependencies=dependencies or [],
            access_status=access_status,
            redacted_content=redacted_content,
            metadata=metadata,
            principal_id=_context()[0],
            scopes={"knowledge:packages:write"},
        ),
        write=True,
        required_scope="knowledge:packages:write",
    )


@mcp.tool()
def resolve_research_package_closure(
    namespace: str, root_ids: list[str], limit: int = 10000
) -> dict:
    """Resolve deterministic dependency closure with redactions and omissions."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c, initialize=False).closure(
            namespace, root_ids, limit=limit, scopes={"knowledge:packages:read"}
        ),
        required_scope="knowledge:packages:read",
    )


@mcp.tool()
def build_research_package(
    namespace: str,
    package_id: str,
    root_ids: list[str],
    allow_partial: bool = False,
    cancel_requested: bool = False,
    limit: int = 10000,
) -> dict:
    """Build deterministic content-addressed bytes for a research package."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c).build(
            namespace,
            package_id,
            root_ids,
            allow_partial=allow_partial,
            cancel_requested=cancel_requested,
            limit=limit,
            principal_id=_context()[0],
            scopes={"knowledge:packages:write"},
        ),
        write=True,
        required_scope="knowledge:packages:write",
    )


@mcp.tool()
def sign_research_package(
    package: dict[str, Any], private_key_b64: str, key_id: str, key_version: str
) -> dict:
    """Attach a detached Ed25519 signature; key material is never persisted."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c, initialize=False).sign(
            package, private_key_b64, key_id=key_id, key_version=key_version
        ),
        required_scope="knowledge:packages:read",
    )


@mcp.tool()
def encrypt_research_package(
    package: dict[str, Any], recipient_key_b64: str, recipient_id: str, key_version: str
) -> dict:
    """Create an AES-256-GCM offline package envelope without persisting keys."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c, initialize=False).encrypt(
            package,
            recipient_key_b64,
            recipient_id=recipient_id,
            key_version=key_version,
        ),
        required_scope="knowledge:packages:read",
    )


@mcp.tool()
def decrypt_research_package(
    envelope: dict[str, Any],
    recipient_key_b64: str,
    recipient_id: str,
    max_bytes: int = 50000000,
) -> dict:
    """Decrypt and authenticate a bounded research package envelope."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c, initialize=False).decrypt(
            envelope, recipient_key_b64, recipient_id=recipient_id, max_bytes=max_bytes
        ),
        required_scope="knowledge:packages:read",
    )


@mcp.tool()
def inspect_research_package(package: dict[str, Any]) -> dict:
    """Inspect package compatibility, member types, and disclosed omissions."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c, initialize=False).inspect(
            package, scopes={"knowledge:packages:read"}
        ),
        required_scope="knowledge:packages:read",
    )


@mcp.tool()
def verify_research_package(
    package: dict[str, Any],
    public_keys: dict[str, str] | None = None,
    require_signature: bool = False,
) -> dict:
    """Verify checksums and optional Ed25519 signature fully offline."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c, initialize=False).verify(
            package, public_keys=public_keys, require_signature=require_signature
        ),
        required_scope="knowledge:packages:read",
    )


@mcp.tool()
def set_research_package_trust_policy(
    namespace: str,
    public_keys: dict[str, Any],
    require_signature: bool = True,
    expected_revision: int = 0,
) -> dict:
    """Version the trusted signer policy for an isolated import namespace."""
    from src.kb.research_packages import ResearchPackageStore, TRUST_SCOPE

    return _safe(
        lambda c: ResearchPackageStore(c).set_trust_policy(
            namespace, public_keys, require_signature=require_signature,
            expected_revision=expected_revision, principal_id=_context()[0], scopes={TRUST_SCOPE},
        ),
        write=True,
        required_scope=TRUST_SCOPE,
    )


@mcp.tool()
def import_research_package(
    package: dict[str, Any],
    target_namespace: str,
    trusted_recipe_ids: list[str] | None = None,
    cancel_requested: bool = False,
    public_keys: dict[str, Any] | None = None,
    require_signature: bool = False,
) -> dict:
    """Import verified members atomically into an isolated import namespace."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c).import_package(
            package,
            target_namespace,
            trusted_recipe_ids=trusted_recipe_ids or [],
            cancel_requested=cancel_requested,
            public_keys=public_keys,
            require_signature=require_signature,
            principal_id=_context()[0],
            scopes={"knowledge:packages:import"},
        ),
        write=True,
        required_scope="knowledge:packages:import",
    )


@mcp.tool()
def replay_research_package(
    target_namespace: str, import_id: str, allow_executable: bool = False
) -> dict:
    """Replay imported content deterministically without trusting recipes by default."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c, initialize=False).replay(
            target_namespace,
            import_id,
            allow_executable=allow_executable,
            scopes={"knowledge:packages:read"},
        ),
        required_scope="knowledge:packages:read",
    )


@mcp.tool()
def rollback_research_package_import(target_namespace: str, import_id: str) -> dict:
    """Atomically remove an isolated import while retaining its receipt."""
    from src.kb.research_packages import ResearchPackageStore

    return _safe(
        lambda c: ResearchPackageStore(c).rollback(
            target_namespace,
            import_id,
            principal_id=_context()[0],
            scopes={"knowledge:packages:import"},
        ),
        write=True,
        required_scope="knowledge:packages:import",
    )


@mcp.tool()
def register_research_analysis(namespace: str, request_key: str, manifest: dict[str, Any]) -> dict:
    """Freeze exact dataset slices, notebook code, parameters, environment identity, and execution limits."""
    from src.kb.research_analysis import ResearchAnalysisStore, WRITE_SCOPE
    return _safe(lambda c: ResearchAnalysisStore(c).register(namespace, request_key, manifest,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=WRITE_SCOPE)


@mcp.tool()
def execute_research_analysis(namespace: str, analysis_id: str, request_key: str) -> dict:
    """Execute pinned code in a bounded rootless container with no network or inherited credentials."""
    from src.kb.research_analysis import ResearchAnalysisStore, EXECUTE_SCOPE
    return _safe(lambda c: ResearchAnalysisStore(c).execute(namespace, analysis_id, request_key,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=EXECUTE_SCOPE)


@mcp.tool()
def inspect_research_analysis(namespace: str, analysis_id: str) -> dict:
    """Inspect a registered manifest under current owner and input access."""
    from src.kb.research_analysis import ResearchAnalysisStore, READ_SCOPE
    return _safe(lambda c: ResearchAnalysisStore(c, initialize=False).inspect(namespace, analysis_id,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=READ_SCOPE)


@mcp.tool()
def list_research_analysis_runs(namespace: str, analysis_id: str, offset: int = 0, limit: int = 100) -> dict:
    """Page through persisted runs for a registered analysis."""
    from src.kb.research_analysis import ResearchAnalysisStore, READ_SCOPE
    return _safe(lambda c: ResearchAnalysisStore(c, initialize=False).list_runs(namespace, analysis_id, offset=offset, limit=limit,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=READ_SCOPE)


@mcp.tool()
def inspect_research_analysis_run(namespace: str, run_id: str) -> dict:
    """Inspect execution status, outputs, isolation receipt, and cell provenance."""
    from src.kb.research_analysis import ResearchAnalysisStore, READ_SCOPE
    return _safe(lambda c: ResearchAnalysisStore(c, initialize=False).inspect_run(namespace, run_id,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=READ_SCOPE)


@mcp.tool()
def cancel_research_analysis_run(namespace: str, run_id: str) -> dict:
    """Request durable cancellation of a running notebook."""
    from src.kb.research_analysis import ResearchAnalysisStore, EXECUTE_SCOPE
    return _safe(lambda c: ResearchAnalysisStore(c, initialize=False).cancel(namespace, run_id,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=EXECUTE_SCOPE)


@mcp.tool()
def recover_research_analysis_run(namespace: str, run_id: str) -> dict:
    """Publish staged results or mark an interrupted run after its hard deadline."""
    from src.kb.research_analysis import ResearchAnalysisStore, EXECUTE_SCOPE
    return _safe(lambda c: ResearchAnalysisStore(c, initialize=False).recover(namespace, run_id,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=EXECUTE_SCOPE)


@mcp.tool()
def export_research_analysis(namespace: str, run_id: str) -> dict:
    """Export pinned code, outputs, permitted inputs, and explicit omissions."""
    from src.kb.research_analysis import ResearchAnalysisStore, READ_SCOPE
    return _safe(lambda c: ResearchAnalysisStore(c, initialize=False).export(namespace, run_id,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=READ_SCOPE)


@mcp.tool()
def export_research_analysis_package(namespace: str, run_id: str) -> dict:
    """Build an offline research package without persisting private input copies."""
    from src.kb.research_analysis import ResearchAnalysisStore, READ_SCOPE
    return _safe(lambda c: ResearchAnalysisStore(c, initialize=False).export_package(namespace, run_id,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope=READ_SCOPE)


@mcp.tool()
def compare_research_analysis_runs(namespace: str, left_run_id: str, right_run_id: str,
                                   absolute_tolerance: float = 0.0, relative_tolerance: float = 0.0) -> dict:
    """Compare completed outputs and provenance using explicit numeric tolerances."""
    from src.kb.research_analysis import ResearchAnalysisStore, READ_SCOPE, compare_analysis_outputs
    def operation(c):
        store = ResearchAnalysisStore(c, initialize=False)
        auth = {"principal_id": _context()[0], "scopes": _context()[1]}
        left = store.inspect_run(namespace, left_run_id, **auth)
        right = store.inspect_run(namespace, right_run_id, **auth)
        return compare_analysis_outputs(left["result"], right["result"],
            absolute_tolerance=absolute_tolerance, relative_tolerance=relative_tolerance)
    return _safe(operation, write=True, required_scope=READ_SCOPE)


@mcp.tool()
def assess_authored_report_changes(namespace: str, report_id: str) -> dict:
    """Poll committed evidence dependencies and persist a focused report-change assessment."""
    from src.kb.report_updates import ReportUpdateStore
    return _safe(lambda c: ReportUpdateStore(c).assess(namespace, report_id,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope="knowledge:reports:write")


@mcp.tool()
def propose_authored_report_edit(namespace: str, assessment_id: str, assertion_id: str,
                                 replacement: dict[str, Any] | None = None) -> dict:
    """Propose an evidence review notice or authored replacement for one affected assertion."""
    from src.kb.report_updates import ReportUpdateStore
    return _safe(lambda c: ReportUpdateStore(c).propose(namespace, assessment_id, assertion_id, replacement=replacement,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope="knowledge:reports:write")


@mcp.tool()
def inspect_authored_report_edit(namespace: str, proposal_id: str) -> dict:
    """Inspect an individual report proposal and its evidence under current access."""
    from src.kb.report_updates import ReportUpdateStore
    return _safe(lambda c: ReportUpdateStore(c, initialize=False).inspect_proposal(namespace, proposal_id,
        principal_id=_context()[0], scopes=_context()[1]), required_scope="knowledge:reports:read")


@mcp.tool()
def decide_authored_report_edit(namespace: str, proposal_id: str, decision: str, rationale: str) -> dict:
    """Accept or reject one edit; acceptance preserves report history and checks author/evidence conflicts."""
    from src.kb.report_updates import ReportUpdateStore
    return _safe(lambda c: ReportUpdateStore(c).decide_proposal(namespace, proposal_id, decision, rationale,
        principal_id=_context()[0], scopes=_context()[1]), write=True, required_scope="knowledge:reports:write")


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)
