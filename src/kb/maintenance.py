"""Continuous, lease-safe maintenance from source schedules to query generations.

The orchestrator composes the existing source-pack runtime, reference workflow,
artifact graph, and subscription watermark stores.  Its own state is deliberately
small: jobs and fenced leases, append-only attempts and generations, processing
offsets, change events, and credential-safe audit records.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackConformance, _load
from src.kb.artifacts import ArtifactGraph
from src.kb.derived_revisions import (
    DerivedRevisionStore,
    maintenance_observations,
)
from src.kb.subscriptions import SubscriptionStore
from src.kb.workflows import WorkflowStore, reference_handlers, reference_manifest, production_handlers

JOB_REQUEST_CONTRACT = "noesis-maintenance-job-request-v1"
JOB_RECEIPT_CONTRACT = "noesis-maintenance-job-receipt-v1"
GENERATION_CONTRACT = "noesis-knowledge-generation-v1"
EVENT_CONTRACT = "noesis-knowledge-generation-event-v1"
HEALTH_CONTRACT = "noesis-maintenance-health-v1"
MAX_LEASE_MS = 300_000
MAX_JOBS_PER_TICK = 100
PROJECTION_KINDS = ("index", "embedding", "entity", "claim", "relation", "summary")
TERMINAL_STATES = frozenset({"complete", "partial", "cancelled", "dead-letter"})
RETRYABLE_CLASSIFICATIONS = frozenset(
    {"rate-limiting", "quota", "transient-availability", "noesis-regression"}
)

_DDL = """
CREATE SEQUENCE IF NOT EXISTS knowledge_maintenance_fence_sequence START 1;
CREATE TABLE IF NOT EXISTS knowledge_maintenance_jobs (
  job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
  pack_id TEXT NOT NULL, scheduled_at_ms BIGINT NOT NULL,
  request_json TEXT NOT NULL, request_hash TEXT NOT NULL,
  status TEXT NOT NULL, available_at_ms BIGINT NOT NULL,
  owner_id TEXT, fencing_token BIGINT, lease_expires_at_ms BIGINT,
  attempts BIGINT NOT NULL, max_attempts BIGINT NOT NULL,
  cancel_requested BOOLEAN NOT NULL, last_error_json TEXT,
  generation_id TEXT, created_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_maintenance_attempts (
  attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt BIGINT NOT NULL,
  owner_id TEXT NOT NULL, fencing_token BIGINT NOT NULL, status TEXT NOT NULL,
  receipt_json TEXT, error_json TEXT, started_at_ms BIGINT NOT NULL,
  completed_at_ms BIGINT, UNIQUE(job_id,attempt)
);
CREATE TABLE IF NOT EXISTS knowledge_maintenance_offsets (
  pack_id TEXT PRIMARY KEY, source_watermark BIGINT NOT NULL,
  workflow_watermark BIGINT NOT NULL, artifact_watermark BIGINT NOT NULL,
  generation BIGINT NOT NULL, generation_id TEXT NOT NULL,
  updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_maintenance_generations (
  generation_id TEXT PRIMARY KEY, pack_id TEXT NOT NULL,
  generation BIGINT NOT NULL, source_watermark BIGINT NOT NULL,
  workflow_run_id TEXT NOT NULL, workflow_watermark BIGINT NOT NULL,
  artifact_watermark BIGINT NOT NULL, status TEXT NOT NULL,
  receipt_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
  committed_at_ms BIGINT NOT NULL, UNIQUE(pack_id,generation),
  UNIQUE(pack_id,source_watermark)
);
CREATE TABLE IF NOT EXISTS knowledge_maintenance_events (
  event_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL UNIQUE,
  pack_id TEXT NOT NULL, event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL, status TEXT NOT NULL,
  published_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_maintenance_schedule_pauses (
  pack_id TEXT PRIMARY KEY, paused BOOLEAN NOT NULL,
  principal_id TEXT NOT NULL, reason TEXT NOT NULL, updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_maintenance_audit (
  event_id TEXT PRIMARY KEY, job_id TEXT, pack_id TEXT,
  principal_id TEXT NOT NULL, action TEXT NOT NULL,
  detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_maintenance_job_claim
  ON knowledge_maintenance_jobs(status,available_at_ms,scheduled_at_ms);
CREATE INDEX IF NOT EXISTS idx_maintenance_generation_pack
  ON knowledge_maintenance_generations(pack_id,generation);
"""


class MaintenanceError(RuntimeError):
    """Stable, credential-safe maintenance failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _now() -> int:
    return int(time.time() * 1000)


def _safe_error(error: BaseException) -> dict[str, str]:
    code = str(getattr(error, "code", "maintenance_failed"))
    allowed = {
        "authentication_failed",
        "budget_exhausted",
        "cancelled",
        "circuit_open",
        "credential_missing",
        "deadline_exceeded",
        "handler_unavailable",
        "license_not_accepted",
        "mapping_drift",
        "network_policy",
        "pack_disabled",
        "preflight_failed",
        "provider_drift",
        "rate_limited",
        "source_timeout",
        "source_unavailable",
        "stage_timeout",
    }
    if code not in allowed:
        code = "maintenance_failed"
    return {"code": code, "message": code.replace("_", " ")}


def _classification(error: BaseException) -> str:
    code = str(getattr(error, "code", ""))
    if code == "cancelled":
        return "cancelled"
    return SourcePackConformance.classify(error)


def _table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
            [table],
        ).fetchone()
    )


class MaintenanceOrchestrator:
    """Durable coordinator for one DuckDB-backed Noesis deployment."""

    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], int] = _now,
        root: Path | None = None,
        initialize: bool = True,
        execution_mode: str = "production",
        extractor_definition=None,
        extractor_implementation=None,
        embedding_provider=None,
        embedding_configuration=None,
    ) -> None:
        self.conn = conn
        if execution_mode not in {"production", "fixture"}:
            raise MaintenanceError("invalid_configuration", "execution_mode must be production or fixture")
        self.execution_mode = execution_mode
        self.extractor_definition, self.extractor_implementation = extractor_definition, extractor_implementation
        self.embedding_provider, self.embedding_configuration = embedding_provider, embedding_configuration
        self.now = now
        self.root = (root or Path(__file__).resolve().parents[2]).resolve()
        self._local_cancel: dict[str, threading.Event] = {}
        if initialize:
            conn.execute(_DDL)
            SourcePackRuntime(conn)
            WorkflowStore(conn)
            ArtifactGraph(conn)
            DerivedRevisionStore(conn)
            SubscriptionStore(conn)

    def enqueue_due(
        self,
        *,
        at_ms: int | None = None,
        max_catchup: int = 10,
        principal_id: str = "maintenance-worker",
        network: str = "disabled",
    ) -> dict[str, Any]:
        """Materialize bounded due schedule windows as idempotent jobs."""

        at = self.now() if at_ms is None else int(at_ms)
        if not 1 <= int(max_catchup) <= MAX_JOBS_PER_TICK:
            raise MaintenanceError(
                "invalid_budget", "max_catchup must be between 1 and 100"
            )
        if network not in {"disabled", "live"}:
            raise MaintenanceError(
                "invalid_request", "network must be disabled or live"
            )
        rows = self.conn.execute(
            "SELECT s.pack_id,s.schedule_json,s.next_run_at_ms "
            "FROM source_pack_schedules s "
            "LEFT JOIN knowledge_maintenance_schedule_pauses p ON p.pack_id=s.pack_id "
            "WHERE s.enabled=true AND COALESCE(p.paused,false)=false AND s.next_run_at_ms<=? "
            "ORDER BY s.next_run_at_ms,s.pack_id",
            [at],
        ).fetchall()
        created: list[str] = []
        existing: list[str] = []
        for pack_id, encoded, schedule_at in rows:
            schedule = _load(encoded, {})
            interval_ms = int(schedule["interval_s"]) * 1000
            latest = self.conn.execute(
                "SELECT MAX(scheduled_at_ms) FROM knowledge_maintenance_jobs WHERE pack_id=?",
                [pack_id],
            ).fetchone()[0]
            cursor = max(
                int(schedule_at),
                int(latest or 0) + interval_ms if latest else int(schedule_at),
            )
            windows = 0
            while cursor <= at and windows < int(max_catchup):
                request = {
                    "contract": JOB_REQUEST_CONTRACT,
                    "pack_id": pack_id,
                    "scheduled_at_ms": cursor,
                    "network": network,
                    "max_documents": 10_000,
                    "max_attempts": 3,
                }
                request_hash = _digest(request)
                idempotency = _digest([pack_id, cursor, request_hash])
                job_id = "maintenance-job:" + idempotency[:24]
                prior = self.conn.execute(
                    "SELECT job_id FROM knowledge_maintenance_jobs WHERE idempotency_key=?",
                    [idempotency],
                ).fetchone()
                if prior:
                    existing.append(prior[0])
                else:
                    self.conn.execute(
                        "INSERT INTO knowledge_maintenance_jobs VALUES "
                        "(?,?,?,?,?,?,'pending',?,NULL,NULL,NULL,0,?,false,NULL,NULL,?,?)",
                        [
                            job_id,
                            idempotency,
                            pack_id,
                            cursor,
                            _canonical(request),
                            request_hash,
                            cursor,
                            request["max_attempts"],
                            at,
                            at,
                        ],
                    )
                    created.append(job_id)
                    self._audit(
                        job_id,
                        pack_id,
                        principal_id,
                        "enqueue",
                        {"scheduled_at_ms": cursor},
                        at,
                    )
                cursor += interval_ms
                windows += 1
        return {
            "contract": "noesis-maintenance-enqueue-v1",
            "at_ms": at,
            "created": created,
            "existing": existing,
            "bounded": True,
        }

    def due_work(self, *, at_ms: int | None = None, limit: int = 100) -> dict[str, Any]:
        at = self.now() if at_ms is None else int(at_ms)
        capped = min(max(1, int(limit)), MAX_JOBS_PER_TICK)
        rows = self.conn.execute(
            "SELECT job_id,pack_id,scheduled_at_ms,status,available_at_ms,attempts,max_attempts "
            "FROM knowledge_maintenance_jobs WHERE status IN ('pending','retry') "
            "AND available_at_ms<=? ORDER BY scheduled_at_ms,job_id LIMIT ?",
            [at, capped],
        ).fetchall()
        return {
            "contract": "noesis-maintenance-due-work-v1",
            "at_ms": at,
            "jobs": [
                {
                    "job_id": row[0],
                    "pack_id": row[1],
                    "scheduled_at_ms": int(row[2]),
                    "status": row[3],
                    "available_at_ms": int(row[4]),
                    "attempts": int(row[5]),
                    "max_attempts": int(row[6]),
                }
                for row in rows
            ],
        }

    def recover_stale(
        self, *, at_ms: int | None = None, principal_id: str = "maintenance-worker"
    ) -> dict[str, Any]:
        at = self.now() if at_ms is None else int(at_ms)
        rows = self.conn.execute(
            "SELECT job_id,pack_id,attempts,max_attempts FROM knowledge_maintenance_jobs "
            "WHERE status IN ('leased','running') AND lease_expires_at_ms<? ORDER BY job_id",
            [at],
        ).fetchall()
        recovered = dead = 0
        for job_id, pack_id, attempts, max_attempts in rows:
            terminal = int(attempts) >= int(max_attempts)
            status = "dead-letter" if terminal else "retry"
            self.conn.execute(
                "UPDATE knowledge_maintenance_jobs SET status=?,available_at_ms=?,owner_id=NULL,"
                "lease_expires_at_ms=NULL,last_error_json=?,updated_at_ms=? WHERE job_id=?",
                [
                    status,
                    at,
                    _canonical({"code": "lease_expired", "message": "lease expired"}),
                    at,
                    job_id,
                ],
            )
            self.conn.execute(
                "UPDATE knowledge_maintenance_attempts SET status='abandoned',error_json=?,completed_at_ms=? "
                "WHERE job_id=? AND completed_at_ms IS NULL",
                [
                    _canonical({"code": "lease_expired", "message": "lease expired"}),
                    at,
                    job_id,
                ],
            )
            dead += terminal
            recovered += not terminal
            self._audit(
                job_id, pack_id, principal_id, "recover-stale", {"status": status}, at
            )
        return {"at_ms": at, "recovered": recovered, "dead_lettered": dead}

    def claim(
        self,
        owner_id: str,
        *,
        lease_ms: int = 60_000,
        at_ms: int | None = None,
    ) -> dict[str, Any] | None:
        owner = str(owner_id).strip()
        if not owner:
            raise MaintenanceError("invalid_owner", "worker owner identity is required")
        if not 1_000 <= int(lease_ms) <= MAX_LEASE_MS:
            raise MaintenanceError("invalid_lease", "lease must be between 1s and 5m")
        at = self.now() if at_ms is None else int(at_ms)
        self.recover_stale(at_ms=at, principal_id=owner)
        self.conn.execute("BEGIN")
        try:
            row = self.conn.execute(
                "SELECT candidate.job_id FROM knowledge_maintenance_jobs candidate "
                "WHERE candidate.status IN ('pending','retry') AND candidate.available_at_ms<=? "
                "AND NOT EXISTS (SELECT 1 FROM knowledge_maintenance_jobs active "
                "WHERE active.pack_id=candidate.pack_id AND active.status IN ('leased','running')) "
                "AND candidate.cancel_requested=false "
                "ORDER BY candidate.scheduled_at_ms,candidate.job_id LIMIT 1",
                [at],
            ).fetchone()
            if not row:
                self.conn.execute("COMMIT")
                return None
            token = int(
                self.conn.execute(
                    "SELECT nextval('knowledge_maintenance_fence_sequence')"
                ).fetchone()[0]
            )
            changed = self.conn.execute(
                "UPDATE knowledge_maintenance_jobs SET status='leased',owner_id=?,fencing_token=?,"
                "lease_expires_at_ms=?,updated_at_ms=? WHERE job_id=? "
                "AND status IN ('pending','retry') RETURNING job_id",
                [owner, token, at + int(lease_ms), at, row[0]],
            ).fetchone()
            if not changed:
                self.conn.execute("ROLLBACK")
                return None
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.inspect_job(row[0])

    def renew(
        self,
        job_id: str,
        owner_id: str,
        fencing_token: int,
        *,
        lease_ms: int = 60_000,
    ) -> dict[str, Any]:
        now = self.now()
        changed = self.conn.execute(
            "UPDATE knowledge_maintenance_jobs SET lease_expires_at_ms=?,updated_at_ms=? "
            "WHERE job_id=? AND owner_id=? AND fencing_token=? AND status IN ('leased','running') "
            "RETURNING lease_expires_at_ms",
            [now + int(lease_ms), now, job_id, owner_id, int(fencing_token)],
        ).fetchone()
        if not changed:
            raise MaintenanceError("lease_lost", "maintenance lease is no longer owned")
        return {"job_id": job_id, "lease_expires_at_ms": int(changed[0])}

    def release(
        self,
        job_id: str,
        owner_id: str,
        fencing_token: int,
        *,
        reason: str = "worker-release",
    ) -> dict[str, Any]:
        """Relinquish an owned lease without consuming another attempt."""

        now = self.now()
        changed = self.conn.execute(
            "UPDATE knowledge_maintenance_jobs SET status='retry',available_at_ms=?,owner_id=NULL,"
            "lease_expires_at_ms=NULL,updated_at_ms=? WHERE job_id=? AND owner_id=? "
            "AND fencing_token=? AND status='leased' RETURNING pack_id",
            [now, now, job_id, owner_id, int(fencing_token)],
        ).fetchone()
        if not changed:
            raise MaintenanceError("lease_lost", "maintenance lease is no longer owned")
        self._audit(
            job_id, changed[0], owner_id, "release", {"reason": str(reason)[:120]}, now
        )
        return {"job_id": job_id, "status": "retry", "released": True}

    def set_schedule_paused(
        self,
        pack_id: str,
        paused: bool,
        *,
        principal_id: str,
        reason: str = "operator",
    ) -> dict[str, Any]:
        SourcePackRuntime(self.conn, initialize=False)._manifest(pack_id)
        now = self.now()
        safe_reason = str(reason)[:120]
        self.conn.execute(
            "INSERT OR REPLACE INTO knowledge_maintenance_schedule_pauses VALUES (?,?,?,?,?)",
            [pack_id, bool(paused), principal_id, safe_reason, now],
        )
        self._audit(
            None,
            pack_id,
            principal_id,
            "pause" if paused else "resume",
            {"reason": safe_reason},
            now,
        )
        return {
            "pack_id": pack_id,
            "paused": bool(paused),
            "reason": safe_reason,
            "updated_at_ms": now,
        }

    def cancel(self, job_id: str, *, principal_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT pack_id,status FROM knowledge_maintenance_jobs WHERE job_id=?",
            [job_id],
        ).fetchone()
        if not row:
            raise MaintenanceError("not_found", "maintenance job does not exist")
        event = self._local_cancel.get(job_id)
        if event:
            event.set()
        now = self.now()
        status = row[1]
        next_status = (
            "cancelled" if status in {"pending", "retry", "leased"} else status
        )
        self.conn.execute(
            "UPDATE knowledge_maintenance_jobs SET cancel_requested=true,status=?,updated_at_ms=? WHERE job_id=?",
            [next_status, now, job_id],
        )
        self._audit(
            job_id, row[0], principal_id, "cancel", {"prior_status": status}, now
        )
        return {"job_id": job_id, "cancel_requested": True, "status": next_status}

    def retry(self, job_id: str, *, principal_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT pack_id,status FROM knowledge_maintenance_jobs WHERE job_id=?",
            [job_id],
        ).fetchone()
        if not row:
            raise MaintenanceError("not_found", "maintenance job does not exist")
        if row[1] not in {"failed", "dead-letter", "cancelled"}:
            raise MaintenanceError(
                "invalid_state", "only terminal failed jobs can be retried"
            )
        now = self.now()
        self.conn.execute(
            "UPDATE knowledge_maintenance_jobs SET status='retry',available_at_ms=?,cancel_requested=false,"
            "owner_id=NULL,lease_expires_at_ms=NULL,last_error_json=NULL,max_attempts=attempts+3,updated_at_ms=? "
            "WHERE job_id=?",
            [now, now, job_id],
        )
        self._audit(job_id, row[0], principal_id, "retry", {}, now)
        return self.inspect_job(job_id)

    def _cancelled(self, job_id: str, external: Callable[[], bool] | None) -> bool:
        local = self._local_cancel.get(job_id)
        if local and local.is_set():
            return True
        if external and external():
            return True
        row = self.conn.execute(
            "SELECT cancel_requested FROM knowledge_maintenance_jobs WHERE job_id=?",
            [job_id],
        ).fetchone()
        return bool(row and row[0])

    def run_once(
        self,
        owner_id: str,
        *,
        principal_id: str | None = None,
        lease_ms: int = 60_000,
        adapter_provider: Callable[[str], Mapping[str, Any]] | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
        dns_resolver: Callable[[str], Sequence[str]] | None = None,
        cancelled: Callable[[], bool] | None = None,
        fail_after_phase: str | None = None,
    ) -> dict[str, Any]:
        """Claim and execute one job through a committed knowledge generation."""

        claimed = self.claim(owner_id, lease_ms=lease_ms)
        if claimed is None:
            return {
                "contract": JOB_RECEIPT_CONTRACT,
                "status": "idle",
                "owner_id": owner_id,
            }
        job_id = claimed["job_id"]
        token = int(claimed["fencing_token"])
        attempt = int(claimed["attempts"]) + 1
        actor = principal_id or owner_id
        now = self.now()
        attempt_id = "maintenance-attempt:" + _digest([job_id, attempt])[:24]
        self.conn.execute(
            "UPDATE knowledge_maintenance_jobs SET status='running',attempts=?,updated_at_ms=? "
            "WHERE job_id=? AND owner_id=? AND fencing_token=?",
            [attempt, now, job_id, owner_id, token],
        )
        self.conn.execute(
            "INSERT INTO knowledge_maintenance_attempts VALUES (?,?,?,?,?,'running',NULL,NULL,?,NULL)",
            [attempt_id, job_id, attempt, owner_id, token, now],
        )
        event = self._local_cancel.setdefault(job_id, threading.Event())
        try:
            result = self._execute_job(
                claimed,
                owner_id=owner_id,
                fencing_token=token,
                principal_id=actor,
                adapter_provider=adapter_provider,
                secret_resolver=secret_resolver,
                dns_resolver=dns_resolver,
                cancelled=lambda: event.is_set() or self._cancelled(job_id, cancelled),
                fail_after_phase=fail_after_phase,
            )
            completed = self.now()
            status = (
                "partial" if result["generation"]["status"] == "partial" else "complete"
            )
            changed = self.conn.execute(
                "UPDATE knowledge_maintenance_jobs SET status=?,owner_id=NULL,lease_expires_at_ms=NULL,"
                "generation_id=?,last_error_json=NULL,updated_at_ms=? WHERE job_id=? AND owner_id=? "
                "AND fencing_token=? RETURNING job_id",
                [
                    status,
                    result["generation"]["generation_id"],
                    completed,
                    job_id,
                    owner_id,
                    token,
                ],
            ).fetchone()
            if not changed:
                raise MaintenanceError(
                    "lease_lost", "stale worker cannot commit job completion"
                )
            receipt = {
                "contract": JOB_RECEIPT_CONTRACT,
                "job_id": job_id,
                "attempt_id": attempt_id,
                "attempt": attempt,
                "owner_id": owner_id,
                "fencing_token": token,
                "status": status,
                **result,
                "completed_at_ms": completed,
            }
            self.conn.execute(
                "UPDATE knowledge_maintenance_attempts SET status=?,receipt_json=?,completed_at_ms=? "
                "WHERE attempt_id=?",
                [status, _canonical(receipt), completed, attempt_id],
            )
            self._audit(
                job_id,
                claimed["pack_id"],
                actor,
                "complete",
                {
                    "status": status,
                    "generation_id": result["generation"]["generation_id"],
                },
                completed,
            )
            return receipt
        except Exception as exc:  # noqa: BLE001 - classify every worker failure durably
            failed = self.now()
            summary = _safe_error(exc)
            classification = _classification(exc)
            retryable = classification in RETRYABLE_CLASSIFICATIONS
            cancelled_state = summary["code"] == "cancelled" or self._cancelled(
                job_id, cancelled
            )
            if cancelled_state:
                status = "cancelled"
                available = failed
            elif retryable and attempt < int(claimed["max_attempts"]):
                status = "retry"
                available = failed + min(60_000, 1_000 * 2 ** (attempt - 1))
            else:
                status = (
                    "dead-letter"
                    if attempt >= int(claimed["max_attempts"])
                    else "failed"
                )
                available = failed
            error = {
                **summary,
                "classification": classification,
                "retryable": retryable,
            }
            self.conn.execute(
                "UPDATE knowledge_maintenance_jobs SET status=?,available_at_ms=?,owner_id=NULL,"
                "lease_expires_at_ms=NULL,last_error_json=?,updated_at_ms=? WHERE job_id=? "
                "AND owner_id=? AND fencing_token=?",
                [status, available, _canonical(error), failed, job_id, owner_id, token],
            )
            self.conn.execute(
                "UPDATE knowledge_maintenance_attempts SET status=?,error_json=?,completed_at_ms=? WHERE attempt_id=?",
                [status, _canonical(error), failed, attempt_id],
            )
            self._audit(
                job_id,
                claimed["pack_id"],
                actor,
                "attempt-failed",
                {"status": status, "error": error},
                failed,
            )
            return {
                "contract": JOB_RECEIPT_CONTRACT,
                "job_id": job_id,
                "attempt_id": attempt_id,
                "attempt": attempt,
                "owner_id": owner_id,
                "fencing_token": token,
                "status": status,
                "error": error,
                "retry_at_ms": available if status == "retry" else None,
                "completed_at_ms": failed,
            }
        finally:
            self._local_cancel.pop(job_id, None)

    def _execute_job(
        self,
        job: Mapping[str, Any],
        *,
        owner_id: str,
        fencing_token: int,
        principal_id: str,
        adapter_provider: Callable[[str], Mapping[str, Any]] | None,
        secret_resolver: Callable[[str], str | None] | None,
        dns_resolver: Callable[[str], Sequence[str]] | None,
        cancelled: Callable[[], bool],
        fail_after_phase: str | None,
    ) -> dict[str, Any]:
        request = dict(job["request"])
        pack_id = str(job["pack_id"])
        runtime = SourcePackRuntime(self.conn)
        manifest, enabled = runtime._manifest(pack_id)
        if not enabled:
            raise MaintenanceError("pack_disabled", "scheduled source pack is disabled")
        adapters = dict(adapter_provider(pack_id)) if adapter_provider else None
        source_receipts = []
        run_ids = []
        for source in manifest["sources"]:
            if cancelled():
                raise MaintenanceError("cancelled", "maintenance job was cancelled")
            required = bool(dict(source.get("health") or {}).get("required", False))
            run_request = {
                "pack_id": pack_id,
                "run_key": f"maintenance:{int(job['scheduled_at_ms'])}:{source['source_id']}",
                "operation": source["operations"][0],
                "source_ids": [source["source_id"]],
                "required_sources": [source["source_id"]] if required else [],
                "network": request["network"],
            }
            receipt = runtime.run(
                run_request,
                principal_id=principal_id,
                adapters=None
                if adapters is None
                else {source["source_id"]: adapters[source["source_id"]]},
                secret_resolver=secret_resolver,
                dns_resolver=dns_resolver,
                cancelled=cancelled,
                advance_schedule=False,
            )
            source_receipts.append(receipt)
            run_ids.append(receipt["run_id"])
        source_watermarks = [
            int(item["watermark"]) for item in source_receipts if item.get("watermark")
        ]
        if not source_watermarks:
            raise MaintenanceError(
                "source_unavailable", "no source reached a committed watermark"
            )
        source_watermark = max(source_watermarks)
        self.renew(job["job_id"], owner_id, fencing_token)
        if fail_after_phase == "source":
            raise MaintenanceError(
                "maintenance_failed", "injected maintenance interruption"
            )

        from src.ingestion.revisions import DocumentRevisionStore

        prior_offset = self.conn.execute(
            "SELECT source_watermark FROM knowledge_maintenance_offsets WHERE pack_id=?",
            [pack_id],
        ).fetchone()
        delta_from = int(prior_offset[0] if prior_offset else 0) + 1
        delta = DocumentRevisionStore(self.conn, initialize=False).delta(
            pack_id,
            from_watermark=delta_from,
            to_watermark=source_watermark,
            limit=int(request["max_documents"]),
        )
        if delta.get("next_cursor"):
            raise MaintenanceError(
                "budget_exhausted", "committed delta exceeds max_documents"
            )
        changes = list(delta["changes"])
        actionable_changes = [
            item for item in changes if item["change_kind"] != "unchanged"
        ]
        documents = self._documents_for_runs(
            [item["run_id"] for item in actionable_changes],
            int(request["max_documents"]),
        )
        namespace = "maintenance:" + pack_id
        workflow_manifest = reference_manifest(namespace)
        workflow_manifest["workflow_id"] = "knowledge-maintenance-" + self.execution_mode
        workflow_manifest["domains"] = list(manifest["domains"])
        workflow_manifest["stages"] = workflow_manifest["stages"][:4]
        workflow_manifest["capabilities"] = [
            stage["capability"] for stage in workflow_manifest["stages"]
        ]
        workflow = WorkflowStore(self.conn).execute(
            workflow_manifest,
            reference_handlers(self.conn, principal_id=principal_id) if self.execution_mode == "fixture" else production_handlers(
                self.conn, principal_id=principal_id, extractor_definition=self.extractor_definition,
                extractor_implementation=self.extractor_implementation),
            {
                "documents": documents,
                "pipeline_configuration": {"execution_mode": self.execution_mode,
                                           "extractor_definition": self.extractor_definition},
                "source_pack": {
                    "pack_id": pack_id,
                    "version": manifest["version"],
                    "manifest_hash": manifest["manifest_hash"],
                    "watermark": source_watermark,
                    "run_ids": sorted(run_ids),
                },
                "coverage": {
                    "complete": all(
                        item["status"] == "complete" for item in source_receipts
                    ),
                    "sources": len(source_receipts),
                    "documents": len(documents),
                    "changes": len(changes),
                },
                "delta": {
                    "contract": delta["contract"],
                    "from_watermark": delta["from_watermark"],
                    "to_watermark": delta["to_watermark"],
                    "delta_hash": delta["delta_hash"],
                    "counts": delta["counts"],
                },
            },
            run_key=f"maintenance:{pack_id}:{source_watermark}",
            cancelled=cancelled,
        )
        workflow_watermark = int(workflow["watermark"]["watermark"])
        self.renew(job["job_id"], owner_id, fencing_token)
        if fail_after_phase == "workflow":
            raise MaintenanceError(
                "maintenance_failed", "injected maintenance interruption"
            )

        artifacts = self._refresh_projections(
            namespace,
            pack_id,
            source_watermark,
            documents,
            workflow,
            changes=actionable_changes,
            cancelled=cancelled,
        )
        self.renew(job["job_id"], owner_id, fencing_token)
        if fail_after_phase == "artifacts":
            raise MaintenanceError(
                "maintenance_failed", "injected maintenance interruption"
            )

        generation = self._commit_generation(
            pack_id,
            manifest,
            source_receipts,
            workflow,
            artifacts,
            principal_id=principal_id,
        )
        self._advance_schedule(pack_id, int(job["scheduled_at_ms"]))
        return {
            "source_runs": source_receipts,
            "documents": {
                "selected": len(documents),
                "run_ids": sorted(run_ids),
                "delta": {
                    "from_watermark": delta["from_watermark"],
                    "to_watermark": delta["to_watermark"],
                    "item_count": delta["item_count"],
                    "counts": delta["counts"],
                    "delta_hash": delta["delta_hash"],
                },
            },
            "workflow": {
                "run_id": workflow["run_id"],
                "watermark": workflow_watermark,
                "status": workflow["status"],
            },
            "artifacts": artifacts,
            "generation": generation,
        }

    def _documents_for_runs(
        self, run_ids: Sequence[str], limit: int
    ) -> list[dict[str, Any]]:
        if not run_ids:
            return []
        unique_runs = sorted(set(run_ids))
        placeholders = ",".join("?" for _ in unique_runs)
        rows = self.conn.execute(
            "SELECT r.payload_json,r.revision_id,r.change_kind,r.committed_watermark "
            "FROM document_revision_records r WHERE r.run_id "
            f"IN ({placeholders}) AND r.committed_watermark IS NOT NULL "
            "AND r.lifecycle='active' AND r.change_kind!='unchanged' "
            "ORDER BY r.document_id,r.revision LIMIT ?",
            [*unique_runs, min(max(1, limit), 100_000)],
        ).fetchall()
        values = []
        for payload_json, revision_id, change_kind, generation in rows:
            payload = _load(payload_json, {})
            payload["_revision_id"] = revision_id
            payload["_change_kind"] = change_kind
            payload["_generation"] = int(generation)
            values.append(payload)
        return values

    def _refresh_projections(
        self,
        namespace: str,
        pack_id: str,
        source_watermark: int,
        documents: Sequence[Mapping[str, Any]],
        workflow: Mapping[str, Any],
        *,
        changes: Sequence[Mapping[str, Any]],
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        graph = ArtifactGraph(self.conn)
        started = self.now()
        document_hashes = sorted(str(item["payload_hash"]) for item in changes)
        extraction = dict(workflow.get("state", {}).get("extraction") or {})
        resolution = dict(workflow.get("state", {}).get("resolution") or {})
        derived = DerivedRevisionStore(self.conn, initialize=False,
            fixture_mode=self.execution_mode == "fixture", embedding_provider=self.embedding_provider,
            embedding_configuration=self.embedding_configuration).apply_generation(
            namespace,
            source_watermark,
            maintenance_observations(documents, extraction),
            changes,
            now_ms=started,
        )
        if not changes:
            prior_rows = self.conn.execute(
                "SELECT kind,artifact_id,content_hash FROM knowledge_artifacts "
                "WHERE namespace=? AND status='active' AND kind IN "
                "('index','embedding','entity','claim','relation','summary') "
                "AND logical_id LIKE ? "
                "QUALIFY ROW_NUMBER() OVER (PARTITION BY kind ORDER BY generation DESC)=1",
                [namespace, f"maintenance:{pack_id}:%"],
            ).fetchall()
            if len(prior_rows) == len(PROJECTION_KINDS):
                active = {row[0]: row[1] for row in prior_rows}
                return {
                    "namespace": namespace,
                    "watermark": graph.watermark(namespace),
                    "published": active,
                    "unchanged": sorted(active.values()),
                    "document_hashes": [],
                    "impacted_artifacts": [],
                    "rebuild_receipts": [
                        {
                            "kind": row[0],
                            "status": "unchanged",
                            "artifact_id": row[1],
                            "content_hash": row[2],
                            "input_count": 0,
                            "omissions": ["empty-delta"],
                        }
                        for row in prior_rows
                    ],
                    "compatibility": {
                        "schema": "compatible",
                        "mapping": "compatible",
                        "extractor": "compatible",
                        "embedding_model": "compatible",
                    },
                    "timing": {
                        "started_at_ms": started,
                        "completed_at_ms": self.now(),
                    },
                    "mixed_generations_visible": False,
                    "derived": derived,
                }
        active_rows = self.conn.execute(
            "SELECT revision_id,payload_hash FROM ("
            "SELECT revision_id,payload_hash,lifecycle,ROW_NUMBER() OVER "
            "(PARTITION BY document_id ORDER BY revision DESC) AS ordinal "
            "FROM document_revision_records WHERE pack_id=? "
            "AND committed_watermark IS NOT NULL) committed "
            "WHERE ordinal=1 AND lifecycle='active' ORDER BY revision_id",
            [pack_id],
        ).fetchall()
        active_revisions = [row[0] for row in active_rows]
        common = {
            "pack_id": pack_id,
            "source_watermark": source_watermark,
            "document_revisions": active_revisions,
            "delta_hash": _digest([dict(item) for item in changes]),
            "extraction_hash": _digest(extraction),
            "resolution_hash": _digest(resolution),
        }
        payloads = {
            "index": {**common, "method": "lexical-manifest-v1"},
            "embedding": {**common, "method": "content-hash-vector-manifest-v1"},
            "entity": {
                **common,
                "outputs": extraction.get("outputs", []),
                "object_type": "entity",
            },
            "claim": {
                **common,
                "outputs": extraction.get("outputs", []),
                "object_type": "claim",
            },
            "relation": {**common, "events": resolution.get("events", [])},
            "summary": {
                **common,
                "coverage": workflow.get("state", {}).get("coverage", {}),
            },
        }
        staged: dict[str, tuple[str | None, str]] = {}
        active: dict[str, str] = {}
        receipts: list[dict[str, Any]] = []
        unchanged = []
        now = self.now()
        invalidations = [
            {
                "dependency_id": str(
                    item.get("predecessor_revision_id") or item["revision_id"]
                ),
                "reason": "document-" + str(item["change_kind"]),
            }
            for item in changes
        ]
        impacted = (
            graph.preview_invalidation(namespace, invalidations)
            if invalidations
            else {"affected": [], "order": []}
        )
        for kind in PROJECTION_KINDS:
            if cancelled():
                raise MaintenanceError(
                    "cancelled",
                    "maintenance job was cancelled before artifact publication",
                )
            logical_id = f"maintenance:{pack_id}:{kind}"
            prior = self.conn.execute(
                "SELECT artifact_id,generation,content_hash FROM knowledge_artifacts "
                "WHERE namespace=? AND logical_id=? AND status='active' "
                "ORDER BY generation DESC LIMIT 1",
                [namespace, logical_id],
            ).fetchone()
            content_hash = _digest(payloads[kind])
            if prior and prior[2] == content_hash:
                unchanged.append(prior[0])
                active[kind] = prior[0]
                receipts.append(
                    {
                        "kind": kind,
                        "status": "unchanged",
                        "artifact_id": prior[0],
                        "content_hash": content_hash,
                        "input_count": len(changes),
                        "omissions": [],
                    }
                )
                continue
            dependencies = [
                {
                    "dependency_id": str(revision_id),
                    "kind": "source",
                    "content_hash": str(payload_hash),
                }
                for revision_id, payload_hash in active_rows
            ] or [
                {
                    "dependency_id": f"source-pack:{pack_id}:{source_watermark}",
                    "kind": "source",
                }
            ]
            registered = graph.register(
                namespace,
                kind,
                logical_id,
                payloads[kind],
                configuration={"contract": GENERATION_CONTRACT, "projection": kind},
                producer={"name": "knowledge-maintenance", "version": "1.0.0"},
                dependencies=dependencies,
                status="staged",
                generation=int(prior[1]) + 1 if prior else 1,
                now_ms=now,
            )
            staged[kind] = (prior[0] if prior else None, registered["artifact_id"])
            active[kind] = registered["artifact_id"]
            receipts.append(
                {
                    "kind": kind,
                    "status": "rebuilt",
                    "artifact_id": registered["artifact_id"],
                    "replaces": prior[0] if prior else None,
                    "content_hash": registered["content_hash"],
                    "input_count": len(changes),
                    "omissions": [],
                }
            )
        current_watermark = graph.watermark(namespace)
        if staged:
            self.conn.execute("BEGIN")
            try:
                for old_id, new_id in staged.values():
                    if old_id:
                        self.conn.execute(
                            "UPDATE knowledge_artifacts SET status='superseded' WHERE artifact_id=?",
                            [old_id],
                        )
                    self.conn.execute(
                        "UPDATE knowledge_artifacts SET status='active' WHERE artifact_id=?",
                        [new_id],
                    )
                current_watermark += 1
                rebuild_id = (
                    "maintenance-refresh:"
                    + _digest([namespace, source_watermark, staged])[:24]
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO knowledge_artifact_watermarks VALUES (?,?,?,?)",
                    [namespace, current_watermark, rebuild_id, now],
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {
            "namespace": namespace,
            "watermark": current_watermark,
            "published": active,
            "unchanged": sorted(unchanged),
            "document_hashes": document_hashes,
            "impacted_artifacts": impacted.get("artifacts", []),
            "invalidation_reasons": invalidations,
            "rebuild_receipts": receipts,
            "compatibility": {
                "schema": "compatible",
                "mapping": "compatible",
                "extractor": "compatible",
                "embedding_model": "compatible",
            },
            "timing": {"started_at_ms": started, "completed_at_ms": self.now()},
            "mixed_generations_visible": False,
            "derived": derived,
        }

    def _commit_generation(
        self,
        pack_id: str,
        manifest: Mapping[str, Any],
        source_receipts: Sequence[Mapping[str, Any]],
        workflow: Mapping[str, Any],
        artifacts: Mapping[str, Any],
        *,
        principal_id: str,
    ) -> dict[str, Any]:
        source_watermark = max(
            int(item["watermark"]) for item in source_receipts if item.get("watermark")
        )
        prior = self.conn.execute(
            "SELECT receipt_json FROM knowledge_maintenance_generations WHERE pack_id=? AND source_watermark=?",
            [pack_id, source_watermark],
        ).fetchone()
        if prior:
            result = _load(prior[0], {})
            result["idempotent"] = True
            return result
        offset = self.conn.execute(
            "SELECT generation FROM knowledge_maintenance_offsets WHERE pack_id=?",
            [pack_id],
        ).fetchone()
        number = int(offset[0] if offset else 0) + 1
        workflow_mark = int(workflow["watermark"]["watermark"])
        artifact_mark = int(artifacts["watermark"])
        status = (
            "complete"
            if all(item["status"] == "complete" for item in source_receipts)
            else "partial"
        )
        derived = dict(artifacts.get("derived") or {})
        stable = {
            "contract": GENERATION_CONTRACT,
            "generation_id": "knowledge-generation:"
            + _digest(
                [
                    pack_id,
                    source_watermark,
                    workflow["run_id"],
                    artifact_mark,
                    derived.get("change_hash"),
                ]
            )[:24],
            "pack_id": pack_id,
            "pack_version": manifest["version"],
            "manifest_hash": manifest["manifest_hash"],
            "generation": number,
            "source_watermark": source_watermark,
            "source_runs": [
                {
                    "run_id": item["run_id"],
                    "receipt_hash": item["receipt_hash"],
                    "status": item["status"],
                }
                for item in source_receipts
            ],
            "workflow_run_id": workflow["run_id"],
            "workflow_watermark": workflow_mark,
            "workflow_state_hash": workflow["watermark"]["state_hash"],
            "artifact_watermark": artifact_mark,
            "artifacts": dict(artifacts["published"]),
            "derived": {
                "namespace": derived.get("namespace"),
                "generation": derived.get("generation"),
                "change_hash": derived.get("change_hash"),
                "item_count": derived.get("item_count", 0),
                "counts": dict(derived.get("counts") or {}),
            },
            "identities": {
                "document_hashes": list(artifacts.get("document_hashes", [])),
                "artifact_hashes": sorted(
                    item["content_hash"]
                    for item in artifacts.get("rebuild_receipts", [])
                ),
                "schemas": [
                    "document-batch-v1",
                    "noesis-resolution-batch-v1",
                    "noesis-derived-object-revision-v1",
                ],
                "models": ["content-hash-vector-manifest-v1"],
                "policies": ["source-pack-runtime-v1", "knowledge-maintenance-v1"],
            },
            "status": status,
        }
        receipt_hash = _digest(stable)
        committed = self.now()
        receipt = {**stable, "receipt_hash": receipt_hash, "committed_at_ms": committed}
        event = {
            "contract": EVENT_CONTRACT,
            "event_id": "knowledge-generation-event:"
            + _digest([stable["generation_id"], receipt_hash])[:24],
            "event_type": "knowledge-generation-committed",
            "generation_id": stable["generation_id"],
            "pack_id": pack_id,
            "generation": number,
            "status": status,
            "receipt_hash": receipt_hash,
        }
        # Workflow consolidation watermarks already occupy the maintenance
        # namespace. Generation commits are a distinct monotonic stream.
        namespace = "knowledge-generation:" + pack_id
        self.conn.execute("BEGIN")
        try:
            DerivedRevisionStore(self.conn, initialize=False).publish_generation(
                str(derived["namespace"]), int(derived["generation"])
            )
            self.conn.execute(
                "INSERT INTO knowledge_maintenance_generations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    stable["generation_id"],
                    pack_id,
                    number,
                    source_watermark,
                    workflow["run_id"],
                    workflow_mark,
                    artifact_mark,
                    status,
                    receipt_hash,
                    _canonical(receipt),
                    committed,
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO knowledge_maintenance_offsets VALUES (?,?,?,?,?,?,?)",
                [
                    pack_id,
                    source_watermark,
                    workflow_mark,
                    artifact_mark,
                    number,
                    stable["generation_id"],
                    committed,
                ],
            )
            self.conn.execute(
                "INSERT INTO knowledge_maintenance_events VALUES (?,?,?,?,?,'published',?)",
                [
                    event["event_id"],
                    stable["generation_id"],
                    pack_id,
                    event["event_type"],
                    _canonical(event),
                    committed,
                ],
            )
            SubscriptionStore(self.conn, initialize=False).commit_watermark(
                namespace,
                number,
                kind="ingestion",
                detail={
                    "generation_id": stable["generation_id"],
                    "receipt_hash": receipt_hash,
                    "pack_id": pack_id,
                },
                committed_at_ms=committed,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self._audit(
            None,
            pack_id,
            principal_id,
            "generation-commit",
            {"generation_id": stable["generation_id"], "receipt_hash": receipt_hash},
            committed,
        )
        return receipt

    def _advance_schedule(self, pack_id: str, scheduled_at_ms: int) -> None:
        row = self.conn.execute(
            "SELECT schedule_json,next_run_at_ms FROM source_pack_schedules WHERE pack_id=? AND enabled=true",
            [pack_id],
        ).fetchone()
        if not row:
            return
        interval = int(_load(row[0], {})["interval_s"]) * 1000
        if int(row[1]) <= scheduled_at_ms:
            self.conn.execute(
                "UPDATE source_pack_schedules SET next_run_at_ms=?,updated_at_ms=? WHERE pack_id=? AND next_run_at_ms<=?",
                [scheduled_at_ms + interval, self.now(), pack_id, scheduled_at_ms],
            )

    def drain(
        self,
        owner_id: str,
        *,
        max_jobs: int = 10,
        enqueue: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        capped = min(max(1, int(max_jobs)), MAX_JOBS_PER_TICK)
        if enqueue:
            self.enqueue_due(principal_id=kwargs.get("principal_id") or owner_id)
        receipts = []
        for _ in range(capped):
            receipt = self.run_once(owner_id, **kwargs)
            if receipt["status"] == "idle":
                break
            receipts.append(receipt)
        return {
            "contract": "noesis-maintenance-drain-v1",
            "owner_id": owner_id,
            "jobs": receipts,
            "processed": len(receipts),
            "bounded": True,
        }

    def inspect_job(self, job_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT job_id,idempotency_key,pack_id,scheduled_at_ms,request_json,request_hash,status,"
            "available_at_ms,owner_id,fencing_token,lease_expires_at_ms,attempts,max_attempts,"
            "cancel_requested,last_error_json,generation_id,created_at_ms,updated_at_ms "
            "FROM knowledge_maintenance_jobs WHERE job_id=?",
            [job_id],
        ).fetchone()
        if not row:
            raise MaintenanceError("not_found", "maintenance job does not exist")
        attempts = self.conn.execute(
            "SELECT attempt_id,attempt,owner_id,fencing_token,status,receipt_json,error_json,"
            "started_at_ms,completed_at_ms FROM knowledge_maintenance_attempts WHERE job_id=? ORDER BY attempt",
            [job_id],
        ).fetchall()
        return {
            "contract": "noesis-maintenance-job-v1",
            "job_id": row[0],
            "idempotency_key": row[1],
            "pack_id": row[2],
            "scheduled_at_ms": int(row[3]),
            "request": _load(row[4], {}),
            "request_hash": row[5],
            "status": row[6],
            "available_at_ms": int(row[7]),
            "owner_id": row[8],
            "fencing_token": None if row[9] is None else int(row[9]),
            "lease_expires_at_ms": None if row[10] is None else int(row[10]),
            "attempts": int(row[11]),
            "max_attempts": int(row[12]),
            "cancel_requested": bool(row[13]),
            "last_error": _load(row[14], None),
            "generation_id": row[15],
            "created_at_ms": int(row[16]),
            "updated_at_ms": int(row[17]),
            "attempt_history": [
                {
                    "attempt_id": value[0],
                    "attempt": int(value[1]),
                    "owner_id": value[2],
                    "fencing_token": int(value[3]),
                    "status": value[4],
                    "receipt": _load(value[5], None),
                    "error": _load(value[6], None),
                    "started_at_ms": int(value[7]),
                    "completed_at_ms": None if value[8] is None else int(value[8]),
                }
                for value in attempts
            ],
        }

    def status(self, *, limit: int = 100) -> dict[str, Any]:
        capped = min(max(1, int(limit)), 500)
        rows = self.conn.execute(
            "SELECT job_id FROM knowledge_maintenance_jobs ORDER BY created_at_ms DESC,job_id LIMIT ?",
            [capped],
        ).fetchall()
        counts = {
            row[0]: int(row[1])
            for row in self.conn.execute(
                "SELECT status,COUNT(*) FROM knowledge_maintenance_jobs GROUP BY status ORDER BY status"
            ).fetchall()
        }
        return {
            "contract": "noesis-maintenance-status-v1",
            "counts": counts,
            "jobs": [self.inspect_job(row[0]) for row in rows],
            "health": self.health(),
        }

    def inspect_generation(self, generation_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT receipt_json FROM knowledge_maintenance_generations WHERE generation_id=? AND status IN ('complete','partial')",
            [generation_id],
        ).fetchone()
        if not row:
            raise MaintenanceError(
                "not_found", "committed knowledge generation does not exist"
            )
        return _load(row[0], {})

    def latest_generation(self, pack_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT receipt_json FROM knowledge_maintenance_generations WHERE pack_id=? "
            "AND status IN ('complete','partial') ORDER BY generation DESC LIMIT 1",
            [pack_id],
        ).fetchone()
        return None if row is None else _load(row[0], {})

    def replay_generation(self, generation_id: str) -> dict[str, Any]:
        receipt = self.inspect_generation(generation_id)
        stable = {
            key: receipt[key]
            for key in receipt
            if key not in {"receipt_hash", "committed_at_ms", "idempotent"}
        }
        computed = _digest(stable)
        source_matches = all(
            self.conn.execute(
                "SELECT 1 FROM source_pack_runs WHERE run_id=? AND json_extract_string(receipt_json,'$.receipt_hash')=?",
                [item["run_id"], item["receipt_hash"]],
            ).fetchone()
            for item in receipt["source_runs"]
        )
        workflow_matches = bool(
            self.conn.execute(
                "SELECT 1 FROM knowledge_workflow_watermarks WHERE run_id=? AND watermark=? AND state_hash=?",
                [
                    receipt["workflow_run_id"],
                    receipt["workflow_watermark"],
                    receipt["workflow_state_hash"],
                ],
            ).fetchone()
        )
        event_matches = bool(
            self.conn.execute(
                "SELECT 1 FROM knowledge_maintenance_events WHERE generation_id=? AND status='published'",
                [generation_id],
            ).fetchone()
        )
        derived = dict(receipt.get("derived") or {})
        derived_matches = bool(
            derived
            and self.conn.execute(
                "SELECT 1 FROM derived_object_generations WHERE namespace=? AND generation=? "
                "AND change_hash=? AND item_count=?",
                [
                    derived.get("namespace"),
                    derived.get("generation"),
                    derived.get("change_hash"),
                    derived.get("item_count"),
                ],
            ).fetchone()
        )
        return {
            "contract": "noesis-knowledge-generation-replay-v1",
            "generation_id": generation_id,
            "matched": computed == receipt["receipt_hash"]
            and source_matches
            and workflow_matches
            and event_matches
            and derived_matches,
            "receipt_hash_match": computed == receipt["receipt_hash"],
            "source_receipts_match": source_matches,
            "workflow_watermark_match": workflow_matches,
            "event_match": event_matches,
            "derived_generation_match": derived_matches,
            "expected_hash": receipt["receipt_hash"],
            "computed_hash": computed,
        }

    def generation_lineage(self, generation_id: str) -> dict[str, Any]:
        receipt = self.inspect_generation(generation_id)
        artifact_edges = []
        graph = ArtifactGraph(self.conn, initialize=False)
        for artifact_id in receipt["artifacts"].values():
            artifact_edges.extend(graph.upstream(artifact_id)["edges"])
        derived = dict(receipt.get("derived") or {})
        derived_rows = self.conn.execute(
            "SELECT revision_id FROM derived_object_generation_changes "
            "WHERE namespace=? AND generation=? ORDER BY ordinal",
            [derived.get("namespace"), derived.get("generation")],
        ).fetchall()
        derived_lineage = [
            DerivedRevisionStore(self.conn, initialize=False).lineage(row[0])
            for row in derived_rows
        ]
        return {
            "contract": "noesis-knowledge-generation-lineage-v1",
            "generation_id": generation_id,
            "pack": {
                "pack_id": receipt["pack_id"],
                "version": receipt["pack_version"],
                "manifest_hash": receipt["manifest_hash"],
            },
            "source_runs": receipt["source_runs"],
            "workflow": {
                "run_id": receipt["workflow_run_id"],
                "watermark": receipt["workflow_watermark"],
            },
            "artifacts": receipt["artifacts"],
            "artifact_edges": artifact_edges,
            "derived": derived,
            "derived_lineage": derived_lineage,
            "complete": True,
        }

    def health(self, *, at_ms: int | None = None) -> dict[str, Any]:
        at = self.now() if at_ms is None else int(at_ms)
        stale_after_ms = 300_000
        stale = self.conn.execute(
            "SELECT COUNT(*) FROM knowledge_maintenance_jobs WHERE status IN ('leased','running') "
            "AND lease_expires_at_ms<?",
            [at],
        ).fetchone()[0]
        oldest_due = self.conn.execute(
            "SELECT MIN(scheduled_at_ms) FROM knowledge_maintenance_jobs WHERE status IN ('pending','retry')"
        ).fetchone()[0]
        last = self.conn.execute(
            "SELECT generation_id,pack_id,generation,committed_at_ms FROM knowledge_maintenance_generations "
            "ORDER BY committed_at_ms DESC LIMIT 1"
        ).fetchone()
        failures = self.conn.execute(
            "SELECT COUNT(*) FROM knowledge_maintenance_jobs WHERE status IN ('failed','dead-letter')"
        ).fetchone()[0]
        stuck_stages = self.conn.execute(
            "SELECT COUNT(*) FROM knowledge_workflow_runs WHERE status='running' AND updated_at_ms<?",
            [at - stale_after_ms],
        ).fetchone()[0]
        stale_sources = self.conn.execute(
            "SELECT COUNT(*) FROM source_pack_health WHERE checked_at_ms<? OR status NOT IN ('healthy','complete')",
            [at - stale_after_ms],
        ).fetchone()[0]
        mean_recovery = self.conn.execute(
            "SELECT COALESCE(AVG(completed_at_ms-started_at_ms),0) "
            "FROM knowledge_maintenance_attempts WHERE status='abandoned' AND completed_at_ms IS NOT NULL"
        ).fetchone()[0]
        freshness = 0 if last is None else max(0, at - int(last[3]))
        processing_lag = 0 if oldest_due is None else max(0, at - int(oldest_due))
        status = (
            "degraded"
            if stale or failures or stuck_stages or stale_sources
            else "healthy"
        )
        return {
            "contract": HEALTH_CONTRACT,
            "status": status,
            "at_ms": at,
            "stale_runs": int(stale),
            "stuck_stages": int(stuck_stages),
            "stale_sources": int(stale_sources),
            "failed_jobs": int(failures),
            "schedule_lag_ms": processing_lag,
            "processing_lag_ms": processing_lag,
            "freshness_ms": freshness,
            "mean_recovery_time_ms": int(mean_recovery or 0),
            "last_committed_generation": None
            if last is None
            else {
                "generation_id": last[0],
                "pack_id": last[1],
                "generation": int(last[2]),
                "committed_at_ms": int(last[3]),
            },
        }

    def _audit(
        self,
        job_id: str | None,
        pack_id: str | None,
        principal_id: str,
        action: str,
        detail: Mapping[str, Any],
        now_ms: int,
    ) -> None:
        safe = json.loads(json.dumps(detail))
        event_id = (
            "maintenance-audit:"
            + _digest([job_id, pack_id, principal_id, action, safe, now_ms])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO knowledge_maintenance_audit VALUES (?,?,?,?,?,?,?)",
            [event_id, job_id, pack_id, principal_id, action, _canonical(safe), now_ms],
        )


def fixture_adapter_provider(
    orchestrator: MaintenanceOrchestrator,
) -> Callable[[str], Mapping[str, Any]]:
    """Return a pinned-fixture adapter provider for offline workers and CI."""

    runtime = SourcePackRuntime(orchestrator.conn, initialize=False)
    return lambda pack_id: runtime.fixture_adapters(pack_id, orchestrator.root)
