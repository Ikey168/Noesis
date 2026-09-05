"""Resumable, receipt-bearing Knowledge Engine reference workflows.

The workflow layer deliberately composes existing subsystems.  A stage handler
is ordinary Python supplied by an operator; the durable core only validates the
manifest, bounds execution, records immutable receipts, and publishes a
watermark after the write-side index stage is durable.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

WORKFLOW_CONTRACT = "noesis-knowledge-workflow-v1"
RECEIPT_CONTRACT = "noesis-workflow-stage-receipt-v1"
WATERMARK_CONTRACT = "noesis-workflow-watermark-v1"
RUN_CONTRACT = "noesis-workflow-run-v1"

STAGE_ORDER = ("ingest", "extract", "resolve", "index", "query", "subscribe", "export")
STAGE_CAPABILITIES = {
    "ingest": "documents:ingest",
    "extract": "knowledge:extract",
    "resolve": "knowledge:resolve",
    "index": "knowledge:index",
    "query": "knowledge:query",
    "subscribe": "knowledge:subscribe",
    "export": "namespace:export",
}
RESOURCE_LIMITS = {
    "timeout_ms": 3_600_000,
    "max_items": 1_000_000,
    "max_bytes": 1_000_000_000,
}

_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_workflow_definitions (
  manifest_hash TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, version TEXT NOT NULL,
  namespace TEXT NOT NULL, manifest_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_workflow_runs (
  run_id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, workflow_id TEXT NOT NULL,
  namespace TEXT NOT NULL, run_key TEXT NOT NULL, input_hash TEXT NOT NULL,
  status TEXT NOT NULL, state_json TEXT NOT NULL, error_json TEXT,
  started_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL,
  UNIQUE(manifest_hash, run_key, input_hash)
);
CREATE TABLE IF NOT EXISTS knowledge_workflow_receipts (
  receipt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL,
  ordinal INTEGER NOT NULL, status TEXT NOT NULL, input_hash TEXT NOT NULL,
  output_hash TEXT, output_json TEXT, contracts_json TEXT NOT NULL,
  lineage_json TEXT NOT NULL, warnings_json TEXT NOT NULL,
  started_at_ms BIGINT NOT NULL, completed_at_ms BIGINT,
  UNIQUE(run_id, stage)
);
CREATE TABLE IF NOT EXISTS knowledge_workflow_watermarks (
  namespace TEXT NOT NULL, workflow_id TEXT NOT NULL, watermark BIGINT NOT NULL,
  run_id TEXT NOT NULL UNIQUE, state_hash TEXT NOT NULL, coverage_json TEXT NOT NULL,
  committed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, workflow_id, watermark)
);
CREATE INDEX IF NOT EXISTS idx_workflow_receipts_run
  ON knowledge_workflow_receipts(run_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_workflow_watermark
  ON knowledge_workflow_watermarks(namespace, workflow_id, watermark);
"""


class WorkflowError(RuntimeError):
    """Stable workflow validation or execution failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            value["details"] = self.details
        return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value is None else json.loads(value) if isinstance(value, str) else value


def _now() -> int:
    return int(time.time() * 1000)


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize a workflow manifest without executing it."""

    value = json.loads(json.dumps(manifest))
    required = {"workflow_id", "version", "namespace", "domains", "capabilities", "stages"}
    missing = required - set(value)
    if missing:
        raise WorkflowError("invalid_manifest", f"missing workflow fields: {sorted(missing)}")
    if not all(str(value[field]).strip() for field in ("workflow_id", "version", "namespace")):
        raise WorkflowError("invalid_manifest", "workflow identity, version, and namespace are required")
    if not isinstance(value["domains"], list) or not value["domains"]:
        raise WorkflowError("ambiguous_scope", "at least one explicit domain is required")
    if len(set(value["domains"])) != len(value["domains"]):
        raise WorkflowError("ambiguous_scope", "domains must be unique")
    capabilities = set(value["capabilities"])
    stages = value["stages"]
    if not isinstance(stages, list) or not stages:
        raise WorkflowError("invalid_manifest", "at least one stage is required")
    names = [str(stage.get("name", "")) for stage in stages]
    if len(names) != len(set(names)) or any(name not in STAGE_ORDER for name in names):
        raise WorkflowError("invalid_stage", "stages must be unique supported stage names")
    if names != sorted(names, key=STAGE_ORDER.index):
        raise WorkflowError("invalid_stage_order", "workflow stages are not in canonical order")
    normalized_stages = []
    for ordinal, raw in enumerate(stages):
        stage = dict(raw)
        name = names[ordinal]
        expected = STAGE_CAPABILITIES[name]
        if stage.get("capability") != expected or expected not in capabilities:
            raise WorkflowError(
                "undeclared_capability", f"stage {name!r} requires declared capability {expected!r}"
            )
        if not stage.get("input_contract") or not stage.get("output_contract"):
            raise WorkflowError("incompatible_contract", f"stage {name!r} needs typed contracts")
        resources = {
            "timeout_ms": 30_000,
            "max_items": 10_000,
            "max_bytes": 10_000_000,
            **dict(stage.get("resources") or {}),
        }
        for key, ceiling in RESOURCE_LIMITS.items():
            try:
                resource = int(resources[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise WorkflowError("invalid_budget", f"stage {name!r} has invalid {key}") from exc
            if resource < 1 or resource > ceiling:
                raise WorkflowError("invalid_budget", f"stage {name!r} has unbounded {key}")
            resources[key] = resource
        stage.update(
            {
                "ordinal": ordinal,
                "resources": resources,
                "policy": dict(stage.get("policy") or {}),
            }
        )
        normalized_stages.append(stage)
    value["contract"] = WORKFLOW_CONTRACT
    value["domains"] = sorted(value["domains"])
    value["capabilities"] = sorted(capabilities)
    value["stages"] = normalized_stages
    value["manifest_hash"] = _digest(value)
    return value


@dataclass(frozen=True)
class WorkflowContext:
    run_id: str
    workflow_id: str
    namespace: str
    domains: tuple[str, ...]
    stage: str
    ordinal: int
    resources: dict[str, int]
    policy: dict[str, Any]
    watermark: int | None
    now_ms: int


StageHandler = Callable[[WorkflowContext, Mapping[str, Any]], Mapping[str, Any]]


class WorkflowStore:
    """Durable workflow definitions, checkpoints, receipts, and watermarks."""

    def __init__(self, conn: Any, *, initialize: bool = True) -> None:
        self.conn = conn
        if initialize:
            conn.execute(_DDL)

    def register(self, manifest: Mapping[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
        value = validate_manifest(manifest)
        self.conn.execute(
            "INSERT OR IGNORE INTO knowledge_workflow_definitions VALUES (?,?,?,?,?,?)",
            [
                value["manifest_hash"],
                value["workflow_id"],
                value["version"],
                value["namespace"],
                _canonical(value),
                now_ms or _now(),
            ],
        )
        return value

    def watermark(self, namespace: str, workflow_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT watermark,run_id,state_hash,coverage_json,committed_at_ms "
            "FROM knowledge_workflow_watermarks WHERE namespace=? AND workflow_id=? "
            "ORDER BY watermark DESC LIMIT 1",
            [namespace, workflow_id],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": WATERMARK_CONTRACT,
            "namespace": namespace,
            "workflow_id": workflow_id,
            "watermark": int(row[0]),
            "run_id": row[1],
            "state_hash": row[2],
            "coverage": _load(row[3], {}),
            "committed_at_ms": int(row[4]),
        }

    def _commit_watermark(
        self,
        manifest: Mapping[str, Any],
        run_id: str,
        state: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        existing = self.conn.execute(
            "SELECT watermark FROM knowledge_workflow_watermarks WHERE run_id=?", [run_id]
        ).fetchone()
        if existing:
            current = self.watermark(str(manifest["namespace"]), str(manifest["workflow_id"]))
            assert current is not None
            if current["run_id"] == run_id:
                return current
            row = self.conn.execute(
                "SELECT state_hash,coverage_json,committed_at_ms FROM knowledge_workflow_watermarks "
                "WHERE run_id=?",
                [run_id],
            ).fetchone()
            return {
                "contract": WATERMARK_CONTRACT,
                "namespace": manifest["namespace"],
                "workflow_id": manifest["workflow_id"],
                "watermark": int(existing[0]),
                "run_id": run_id,
                "state_hash": row[0],
                "coverage": _load(row[1], {}),
                "committed_at_ms": int(row[2]),
            }
        namespace, workflow_id = str(manifest["namespace"]), str(manifest["workflow_id"])
        previous = self.watermark(namespace, workflow_id)
        value = 1 if previous is None else int(previous["watermark"]) + 1
        coverage = dict(state.get("coverage") or {"complete": True})
        result = {
            "contract": WATERMARK_CONTRACT,
            "namespace": namespace,
            "workflow_id": workflow_id,
            "watermark": value,
            "run_id": run_id,
            "state_hash": _digest(state),
            "coverage": coverage,
            "committed_at_ms": now_ms,
        }
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO knowledge_workflow_watermarks VALUES (?,?,?,?,?,?,?)",
                [namespace, workflow_id, value, run_id, result["state_hash"], _canonical(coverage), now_ms],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def receipts(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT receipt_id,stage,ordinal,status,input_hash,output_hash,contracts_json,"
            "lineage_json,warnings_json,started_at_ms,completed_at_ms FROM "
            "knowledge_workflow_receipts WHERE run_id=? ORDER BY ordinal",
            [run_id],
        ).fetchall()
        return [
            {
                "contract": RECEIPT_CONTRACT,
                "receipt_id": row[0],
                "run_id": run_id,
                "stage": row[1],
                "ordinal": int(row[2]),
                "status": row[3],
                "input_hash": row[4],
                "output_hash": row[5],
                "contracts": _load(row[6], {}),
                "lineage": _load(row[7], []),
                "warnings": _load(row[8], []),
                "started_at_ms": int(row[9]),
                "completed_at_ms": None if row[10] is None else int(row[10]),
            }
            for row in rows
        ]

    def inspect(self, run_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT manifest_hash,workflow_id,namespace,run_key,input_hash,status,state_json,"
            "error_json,started_at_ms,updated_at_ms FROM knowledge_workflow_runs WHERE run_id=?",
            [run_id],
        ).fetchone()
        if not row:
            raise WorkflowError("not_found", "workflow run does not exist")
        return {
            "contract": RUN_CONTRACT,
            "run_id": run_id,
            "manifest_hash": row[0],
            "workflow_id": row[1],
            "namespace": row[2],
            "run_key": row[3],
            "input_hash": row[4],
            "status": row[5],
            "state": _load(row[6], {}),
            "error": _load(row[7], None),
            "started_at_ms": int(row[8]),
            "updated_at_ms": int(row[9]),
            "receipts": self.receipts(run_id),
            "watermark": self._watermark_for_run(run_id),
        }

    def execute(
        self,
        manifest: Mapping[str, Any],
        handlers: Mapping[str, StageHandler],
        initial: Mapping[str, Any],
        *,
        run_key: str,
        cancelled: Callable[[], bool] | None = None,
        fail_after: int | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Execute or resume a workflow, publishing only after ``index``."""

        definition = self.register(manifest, now_ms=now_ms)
        initial_hash = _digest(initial)
        identity = [definition["manifest_hash"], run_key, initial_hash]
        run_id = "workflow-run:" + _digest(identity)[:24]
        now = now_ms or _now()
        prior = self.conn.execute(
            "SELECT status,state_json FROM knowledge_workflow_runs WHERE run_id=?", [run_id]
        ).fetchone()
        if prior and prior[0] == "completed":
            self._reconcile_index_publication(definition, run_id)
            result = self.inspect(run_id)
            result["idempotent"] = True
            return result
        if prior is None:
            self.conn.execute(
                "INSERT INTO knowledge_workflow_runs VALUES (?,?,?,?,?,?,'running',?,NULL,?,?)",
                [
                    run_id,
                    definition["manifest_hash"],
                    definition["workflow_id"],
                    definition["namespace"],
                    run_key,
                    initial_hash,
                    _canonical(initial),
                    now,
                    now,
                ],
            )
        else:
            self.conn.execute(
                "UPDATE knowledge_workflow_runs SET status='running',error_json=NULL,updated_at_ms=? "
                "WHERE run_id=?",
                [now, run_id],
            )
        state: Mapping[str, Any] = dict(initial)
        completed = {
            row[0]: _load(row[1], {})
            for row in self.conn.execute(
                "SELECT stage,output_json FROM knowledge_workflow_receipts "
                "WHERE run_id=? AND status='completed' ORDER BY ordinal",
                [run_id],
            ).fetchall()
        }
        watermark = self._watermark_for_run(run_id)
        completed_count = 0
        try:
            for stage in definition["stages"]:
                name = stage["name"]
                if name in completed:
                    state = completed[name]
                    if name == "index":
                        watermark = self._reconcile_index_publication(definition, run_id)
                    completed_count += 1
                    continue
                if cancelled and cancelled():
                    raise WorkflowError("cancelled", "workflow cancelled before the next checkpoint")
                handler = handlers.get(name)
                if handler is None:
                    raise WorkflowError("handler_unavailable", f"no handler is registered for {name!r}")
                started = now_ms or _now()
                context = WorkflowContext(
                    run_id=run_id,
                    workflow_id=definition["workflow_id"],
                    namespace=definition["namespace"],
                    domains=tuple(definition["domains"]),
                    stage=name,
                    ordinal=int(stage["ordinal"]),
                    resources=dict(stage["resources"]),
                    policy=dict(stage["policy"]),
                    watermark=None if watermark is None else int(watermark["watermark"]),
                    now_ms=now,
                )
                input_hash = _digest(state)
                output = dict(handler(context, state))
                elapsed_ms = max(0, (now_ms or _now()) - started)
                encoded = _canonical(output).encode()
                item_count = len(output.get("items", output.get("documents", [])))
                if elapsed_ms > stage["resources"]["timeout_ms"]:
                    raise WorkflowError("stage_timeout", f"stage {name!r} exceeded its time budget")
                if len(encoded) > stage["resources"]["max_bytes"]:
                    raise WorkflowError("stage_too_large", f"stage {name!r} exceeded its byte budget")
                if item_count > stage["resources"]["max_items"]:
                    raise WorkflowError("stage_too_many_items", f"stage {name!r} exceeded its item budget")
                completed_at = now_ms or _now()
                output_hash = _digest(output)
                receipt_id = "workflow-receipt:" + _digest([run_id, name, input_hash, output_hash])[:24]
                contracts = {
                    "workflow": WORKFLOW_CONTRACT,
                    "input": stage["input_contract"],
                    "output": stage["output_contract"],
                    **dict(output.get("contract_versions") or {}),
                }
                self.conn.execute("BEGIN")
                try:
                    self.conn.execute(
                        "DELETE FROM knowledge_workflow_receipts WHERE run_id=? AND stage=?",
                        [run_id, name],
                    )
                    self.conn.execute(
                        "INSERT INTO knowledge_workflow_receipts VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [
                            receipt_id,
                            run_id,
                            name,
                            stage["ordinal"],
                            "completed",
                            input_hash,
                            output_hash,
                            _canonical(output),
                            _canonical(contracts),
                            _canonical(output.get("lineage") or []),
                            _canonical(output.get("warnings") or []),
                            started,
                            completed_at,
                        ],
                    )
                    self.conn.execute(
                        "UPDATE knowledge_workflow_runs SET state_json=?,updated_at_ms=? WHERE run_id=?",
                        [_canonical(output), completed_at, run_id],
                    )
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
                state = output
                completed_count += 1
                if name == "index":
                    watermark = self._commit_watermark(definition, run_id, state, completed_at)
                    self._publish_subscription_watermark(watermark)
                if fail_after is not None and completed_count >= fail_after:
                    raise WorkflowError("injected_failure", "deterministic workflow interruption")
            self.conn.execute(
                "UPDATE knowledge_workflow_runs SET status='completed',state_json=?,error_json=NULL,"
                "updated_at_ms=? WHERE run_id=?",
                [_canonical(state), now_ms or _now(), run_id],
            )
        except Exception as exc:
            error = (
                exc.as_dict()
                if isinstance(exc, WorkflowError)
                else {"code": "stage_failed", "message": str(exc)[:300]}
            )
            status = "cancelled" if error["code"] == "cancelled" else "failed"
            self.conn.execute(
                "UPDATE knowledge_workflow_runs SET status=?,error_json=?,updated_at_ms=? WHERE run_id=?",
                [status, _canonical(error), now_ms or _now(), run_id],
            )
            raise
        return self.inspect(run_id)

    def _reconcile_index_publication(
        self, definition: Mapping[str, Any], run_id: str
    ) -> dict[str, Any] | None:
        """Finish publication from the durable index receipt after interruption.

        Both publication operations are idempotent. The receipt is the durable
        intent, including the exact indexed state and its original commit time.
        Never publish a later query/export state as the index generation.
        """
        receipt = self.conn.execute(
            "SELECT output_json,completed_at_ms FROM knowledge_workflow_receipts "
            "WHERE run_id=? AND stage='index' AND status='completed'",
            [run_id],
        ).fetchone()
        if receipt is None:
            return None
        watermark = self._commit_watermark(
            definition, run_id, _load(receipt[0], {}), int(receipt[1])
        )
        self._publish_subscription_watermark(watermark)
        return watermark

    def _watermark_for_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT namespace,workflow_id,watermark,state_hash,coverage_json,committed_at_ms "
            "FROM knowledge_workflow_watermarks WHERE run_id=?",
            [run_id],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": WATERMARK_CONTRACT,
            "namespace": row[0],
            "workflow_id": row[1],
            "watermark": int(row[2]),
            "run_id": run_id,
            "state_hash": row[3],
            "coverage": _load(row[4], {}),
            "committed_at_ms": int(row[5]),
        }

    def _publish_subscription_watermark(self, watermark: Mapping[str, Any]) -> None:
        from src.kb.subscriptions import SubscriptionStore

        SubscriptionStore(self.conn).commit_watermark(
            str(watermark["namespace"]),
            int(watermark["watermark"]),
            kind="consolidation",
            detail={
                "workflow_id": watermark["workflow_id"],
                "run_id": watermark["run_id"],
                "state_hash": watermark["state_hash"],
                "coverage": watermark["coverage"],
            },
            committed_at_ms=int(watermark["committed_at_ms"]),
        )

    def read_stage(self, namespace: str, workflow_id: str, watermark: int, stage: str) -> dict[str, Any]:
        """Read one stage from exactly one committed generation."""

        row = self.conn.execute(
            "SELECT w.run_id,r.output_json,r.output_hash FROM knowledge_workflow_watermarks w "
            "JOIN knowledge_workflow_receipts r ON r.run_id=w.run_id "
            "WHERE w.namespace=? AND w.workflow_id=? AND w.watermark=? AND r.stage=? "
            "AND r.status='completed'",
            [namespace, workflow_id, int(watermark), stage],
        ).fetchone()
        if not row:
            raise WorkflowError("uncommitted_state", "stage is not visible at the requested watermark")
        return {
            "run_id": row[0],
            "watermark": int(watermark),
            "stage": stage,
            "output": _load(row[1], {}),
            "output_hash": row[2],
        }

    def recoverable(self) -> dict[str, Any]:
        """Report interrupted runs and whether a write generation was published."""

        rows = self.conn.execute(
            "SELECT r.run_id,r.status,r.workflow_id,r.namespace,w.watermark "
            "FROM knowledge_workflow_runs r LEFT JOIN knowledge_workflow_watermarks w "
            "ON w.run_id=r.run_id WHERE r.status IN ('running','failed','cancelled') ORDER BY r.run_id"
        ).fetchall()
        return {
            "runs": [
                {
                    "run_id": row[0],
                    "status": row[1],
                    "workflow_id": row[2],
                    "namespace": row[3],
                    "published_watermark": row[4],
                    "safe_to_resume": True,
                }
                for row in rows
            ],
            "mixed_generations_visible": False,
        }


def reference_manifest(namespace: str = "reference") -> dict[str, Any]:
    """Return the canonical seven-stage Knowledge Engine reference manifest."""

    contracts = {
        "ingest": ("document-batch-v1", "document-store-summary-v1"),
        "extract": ("document-store-summary-v1", "noesis-extractor-run-v1"),
        "resolve": ("noesis-extractor-run-v1", "noesis-resolution-batch-v1"),
        "index": ("noesis-resolution-batch-v1", "noesis-derived-artifact-v1"),
        "query": ("noesis-derived-artifact-v1", "noesis-query-result-v1"),
        "subscribe": ("noesis-query-result-v1", "noesis-knowledge-subscription-event-v1"),
        "export": ("noesis-knowledge-subscription-event-v1", "noesis-knowledge-package-v1"),
    }
    stages = [
        {
            "name": name,
            "capability": STAGE_CAPABILITIES[name],
            "input_contract": contracts[name][0],
            "output_contract": contracts[name][1],
            "resources": {"timeout_ms": 30_000, "max_items": 10_000, "max_bytes": 20_000_000},
            "policy": {"required": True},
        }
        for name in STAGE_ORDER
    ]
    return {
        "workflow_id": "knowledge-engine-reference",
        "version": "1.0.0",
        "namespace": namespace,
        "domains": ["economic", "osint", "political", "research", "scientific", "technical"],
        "capabilities": sorted(STAGE_CAPABILITIES.values()),
        "stages": stages,
    }


class _FixtureExtractor:
    def extract(self, value: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        return list(dict(value.get("metadata") or {}).get("knowledge") or [])


def reference_handlers(conn: Any, *, principal_id: str = "reference-runner") -> dict[str, StageHandler]:
    """Compose real subsystem APIs into deterministic offline stage handlers."""

    scopes = {
        "operator",
        "knowledge:subscriptions:read",
        "knowledge:subscriptions:write",
        "knowledge:namespace:export",
    }

    def ingest(context: WorkflowContext, state: Mapping[str, Any]) -> Mapping[str, Any]:
        from src.ingestion.document_store import DocumentStore

        documents = list(state.get("documents") or [])
        store = DocumentStore(conn)
        summary = store.upsert(documents, validate=False)
        stored = []
        for item in documents:
            document = store.get(str(item.get("document_id", "")))
            if document is not None:
                stored.append(document)
        return {
            **state,
            "documents": stored,
            "ingest": summary.as_dict(),
            "document_ids": sorted(str(item["document_id"]) for item in stored),
            "coverage": {"complete": summary.invalid == 0, "invalid": summary.invalid},
            "lineage": [{"source_id": item.get("source_id"), "document_id": item["document_id"]} for item in stored],
        }

    def extract(context: WorkflowContext, state: Mapping[str, Any]) -> Mapping[str, Any]:
        from src.kb.extractors import ExtractorRegistry

        registry = ExtractorRegistry(conn)
        definition = registry.register(
            {
                "name": "reference-fixture",
                "semantic_version": "1.0.0",
                "capabilities": ["claim", "entity", "event", "relation"],
                "accepted_object_types": ["document"],
                "output_schemas": {"claim": "fixture-v1", "entity": "fixture-v1", "event": "fixture-v1", "relation": "fixture-v1"},
                "implementation": {"rule_version": "1.0.0"},
                "configuration": {"source": "metadata.knowledge"},
                "resources": {"network": False},
            },
            _FixtureExtractor(),
        )
        inputs = [
            {
                "id": item["document_id"],
                "object_type": "document",
                "revision": _digest(item),
                "metadata": item.get("metadata") or {},
            }
            for item in state.get("documents") or []
        ]
        result = registry.run(definition["extractor_id"], context.namespace, inputs, now_ms=context.now_ms)
        return {**state, "extraction": result, "contract_versions": {"extractor": definition["extractor_id"]}}

    def resolve(context: WorkflowContext, state: Mapping[str, Any]) -> Mapping[str, Any]:
        from src.kb.events import EventResolver

        resolver = EventResolver(conn)
        resolved = []
        for output in state["extraction"]["outputs"]:
            item = output.get("output") or {}
            if item.get("output_type") != "event":
                continue
            event = dict(item.get("value") or {})
            resolved.append(
                resolver.resolve_report(
                    context.namespace,
                    event,
                    report_id=output["output_id"],
                    now_ms=context.now_ms,
                )
            )
        return {**state, "resolution": {"events": resolved, "count": len(resolved)}}

    def index(context: WorkflowContext, state: Mapping[str, Any]) -> Mapping[str, Any]:
        from src.kb.artifacts import ArtifactGraph
        from src.kb.portable_namespaces import PortableNamespaceStore

        graph = ArtifactGraph(conn)
        portable = PortableNamespaceStore(conn)
        sources = []
        for document in state.get("documents") or []:
            logical_id = "document:" + str(document["document_id"])
            prior = conn.execute(
                "SELECT artifact_id,content_hash FROM knowledge_artifacts WHERE namespace=? "
                "AND logical_id=? AND status='active' ORDER BY generation DESC LIMIT 1",
                [context.namespace, logical_id],
            ).fetchone()
            content_hash = _digest(document)
            if prior and prior[1] == content_hash:
                artifact_id = prior[0]
            else:
                artifact_id = graph.register(
                    context.namespace,
                    "source",
                    logical_id,
                    document,
                    configuration={"contract": "document-ingest-v1"},
                    producer={"name": "reference-ingest", "version": "1.0.0"},
                    dependencies=[{"dependency_id": str(document.get("source_id") or document["document_id"]), "kind": "source"}],
                    now_ms=context.now_ms,
                )["artifact_id"]
            sources.append(artifact_id)
            portable.put_component(
                context.namespace,
                "document",
                str(document["document_id"]),
                document,
                source_id=document.get("source_id"),
                sensitivity=str((document.get("metadata") or {}).get("sensitivity", "public")),
                observed_at_ms=context.now_ms,
            )
        index_artifact = graph.register(
            context.namespace,
            "index",
            "reference-index:" + context.run_id,
            {"documents": sorted(state.get("document_ids") or []), "events": state["resolution"]["events"]},
            configuration={"domains": list(context.domains)},
            producer={"name": "reference-index", "version": "1.0.0"},
            dependencies=[{"dependency_id": item, "kind": "source"} for item in sources],
            now_ms=context.now_ms,
        )
        return {**state, "index": index_artifact, "lineage": [{"artifact_id": item} for item in sources]}

    def query(context: WorkflowContext, state: Mapping[str, Any]) -> Mapping[str, Any]:
        if context.watermark is None:
            raise WorkflowError("uncommitted_state", "query requires a committed write watermark")
        items = []
        for document in sorted(state.get("documents") or [], key=lambda item: item["document_id"]):
            sensitivity = str((document.get("metadata") or {}).get("sensitivity", "public"))
            if sensitivity != "public":
                continue
            items.append(
                {
                    "id": document["document_id"],
                    "title": document.get("title"),
                    "domain": (document.get("metadata") or {}).get("domain"),
                    "citations": [
                        {
                            "document_id": document["document_id"],
                            "source": document.get("source_id"),
                            "url": document.get("url"),
                        }
                    ],
                }
            )
        result = {
            "contract": "noesis-query-result-v1",
            "items": items,
            "coverage": {"complete": bool(state.get("coverage", {}).get("complete", True)), "returned": len(items)},
            "watermark": context.watermark,
        }
        return {**state, "query": result}

    def subscribe(context: WorkflowContext, state: Mapping[str, Any]) -> Mapping[str, Any]:
        from src.kb.subscriptions import SubscriptionStore

        store = SubscriptionStore(conn)
        definition = {
            "namespace": context.namespace,
            "domain": "general",
            "query": {"operation": "search", "text": "reference"},
            "filters": {"domains": list(context.domains)},
            "cadence": {"trigger": "watermark"},
            "delivery": {"kind": "poll"},
        }
        subscription = store.create(
            definition,
            context.run_id + ":subscription",
            principal_id=principal_id,
            scopes=scopes,
        )
        evaluated = store.evaluate(
            subscription["subscription_id"],
            int(context.watermark or 0),
            state["query"],
            principal_id=principal_id,
            scopes=scopes,
            observed_at_ms=context.now_ms,
        )
        events = store.poll(
            subscription["subscription_id"], principal_id=principal_id, scopes=scopes
        )
        return {**state, "subscription": subscription, "evaluation": evaluated, "events": events}

    def export(context: WorkflowContext, state: Mapping[str, Any]) -> Mapping[str, Any]:
        from src.kb.portable_namespaces import PortableNamespaceStore

        store = PortableNamespaceStore(conn)
        store.put_component(
            context.namespace,
            "provenance",
            context.run_id + ":query",
            {"query": state["query"], "events": state["events"]},
            dependencies=state.get("document_ids") or [],
            observed_at_ms=context.now_ms,
        )
        package = store.export(
            context.namespace,
            redaction={"sensitivities": ["private", "restricted"]},
            scopes=scopes,
        )
        verification = store.verify(package)
        return {
            **state,
            "package": package,
            "verification": verification,
            "report": {
                "run_id": context.run_id,
                "watermark": context.watermark,
                "documents": len(state.get("document_ids") or []),
                "query_results": len(state["query"]["items"]),
                "subscription_events": len(state["events"]["events"]),
                "package_hash": verification["package_hash"],
                "verified": verification["valid"],
            },
        }

    return {
        "ingest": ingest,
        "extract": extract,
        "resolve": resolve,
        "index": index,
        "query": query,
        "subscribe": subscribe,
        "export": export,
    }
