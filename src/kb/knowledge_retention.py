"""Verifiable retention, compaction, archival, restore, and dependency-safe GC."""

from __future__ import annotations

from src.kb.retention_coordination import coordinated

import hashlib
from itertools import islice
import json
import time

POLICY_CONTRACT = "noesis-retention-policy-v1"
CHECKPOINT_CONTRACT = "noesis-retention-checkpoint-v1"
ARCHIVE_CONTRACT = "noesis-archive-manifest-v1"
PLAN_CONTRACT = "noesis-retention-gc-plan-v1"
JOB_CONTRACT = "noesis-retention-job-v1"
READ_SCOPE = "knowledge:retention:read"
ADMIN_SCOPE = "knowledge:retention:admin"
EXECUTE_SCOPE = "knowledge:retention:execute"

_DDL = """
CREATE TABLE IF NOT EXISTS retention_policies(policy_id TEXT NOT NULL,namespace TEXT NOT NULL,version BIGINT NOT NULL,parent_policy_id TEXT,rules_json TEXT NOT NULL,status TEXT NOT NULL,created_at_ms BIGINT NOT NULL,PRIMARY KEY(namespace,policy_id,version));
CREATE TABLE IF NOT EXISTS retention_holds(hold_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,object_id TEXT NOT NULL,reason TEXT NOT NULL,expires_at_ms BIGINT,status TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS retention_objects(namespace TEXT NOT NULL,object_id TEXT NOT NULL,object_class TEXT NOT NULL,source_license TEXT,access_class TEXT,created_at_ms BIGINT NOT NULL,value_score DOUBLE NOT NULL,generation BIGINT NOT NULL,policy_id TEXT NOT NULL,policy_version BIGINT NOT NULL,payload_json TEXT NOT NULL,dependencies_json TEXT NOT NULL,pins_json TEXT NOT NULL,status TEXT NOT NULL,PRIMARY KEY(namespace,object_id));
CREATE TABLE IF NOT EXISTS retention_checkpoints(checkpoint_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,generation_start BIGINT NOT NULL,generation_end BIGINT NOT NULL,schema_version TEXT NOT NULL,content_hash TEXT NOT NULL,records_json TEXT NOT NULL,tombstones_json TEXT NOT NULL,status TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS retention_archives(archive_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,checkpoint_id TEXT NOT NULL,storage_json TEXT NOT NULL,encryption_json TEXT NOT NULL,content_hash TEXT NOT NULL,status TEXT NOT NULL,manifest_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS retention_jobs(job_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,job_type TEXT NOT NULL,input_hash TEXT NOT NULL,status TEXT NOT NULL,processed BIGINT NOT NULL,result_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,updated_at_ms BIGINT NOT NULL,UNIQUE(namespace,job_type,input_hash));
CREATE TABLE IF NOT EXISTS retention_audit(audit_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,operation TEXT NOT NULL,object_id TEXT NOT NULL,principal_id TEXT NOT NULL,detail_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
"""


class KnowledgeRetentionError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def _canon(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(v):
    return hashlib.sha256(_canon(v).encode()).hexdigest()


def _load(v, d):
    return d if v is None else json.loads(v) if isinstance(v, str) else v


def _require(scopes, required):
    if required not in scopes and "operator" not in scopes:
        raise KnowledgeRetentionError(
            "unauthorized", f"missing required scope {required}"
        )


def _bound(v, maximum=1000):
    return min(max(int(v), 1), maximum)


class KnowledgeRetentionStore:
    def __init__(self, conn, *, initialize=True, now=None, archive_backend=None):
        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        self.archive_backend = archive_backend
        if initialize:
            conn.execute(_DDL)

    def _audit(self, namespace, operation, object_id, principal_id, detail=None):
        now = self.now()
        detail = dict(detail or {})
        self.conn.execute(
            "INSERT OR IGNORE INTO retention_audit VALUES (?,?,?,?,?,?,?)",
            [
                "retention-audit:"
                + _hash([namespace, operation, object_id, detail, now])[:24],
                namespace,
                operation,
                object_id,
                principal_id,
                _canon(detail),
                now,
            ],
        )

    def register_policy(
        self,
        namespace,
        policy_id,
        version,
        rules,
        *,
        principal_id,
        scopes,
        parent_policy_id=None,
        status="active",
    ):
        _require(scopes, ADMIN_SCOPE)
        if "minimum_age_ms" not in rules:
            raise KnowledgeRetentionError(
                "invalid_policy", "minimum_age_ms is required"
            )
        row = self.conn.execute(
            "SELECT rules_json,parent_policy_id FROM retention_policies WHERE namespace=? AND policy_id=? AND version=?",
            [namespace, policy_id, version],
        ).fetchone()
        if row:
            if _load(row[0], {}) != dict(rules) or row[1] != parent_policy_id:
                raise KnowledgeRetentionError(
                    "policy_version_conflict", "retention policy version is immutable"
                )
            return self.policy(
                namespace, policy_id, version, scopes={READ_SCOPE}, idempotent=True
            )
        now = self.now()
        self.conn.execute(
            "INSERT INTO retention_policies VALUES (?,?,?,?,?,?,?)",
            [
                policy_id,
                namespace,
                version,
                parent_policy_id,
                _canon(dict(rules)),
                status,
                now,
            ],
        )
        self._audit(
            namespace, "register_policy", f"{policy_id}:{version}", principal_id
        )
        return self.policy(namespace, policy_id, version, scopes={READ_SCOPE})

    def policy(self, namespace, policy_id, version, *, scopes, idempotent=False):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT parent_policy_id,rules_json,status FROM retention_policies WHERE namespace=? AND policy_id=? AND version=?",
            [namespace, policy_id, version],
        ).fetchone()
        if not row:
            raise KnowledgeRetentionError(
                "policy_not_found", "retention policy not found"
            )
        rules = {}
        if row[0]:
            parent = self.conn.execute(
                "SELECT version FROM retention_policies WHERE namespace=? AND policy_id=? ORDER BY version DESC LIMIT 1",
                [namespace, row[0]],
            ).fetchone()
            if not parent:
                raise KnowledgeRetentionError(
                    "parent_policy_not_found", "inherited retention policy not found"
                )
            rules.update(
                self.policy(namespace, row[0], parent[0], scopes={READ_SCOPE})[
                    "effective_rules"
                ]
            )
        rules.update(_load(row[1], {}))
        return {
            "contract": POLICY_CONTRACT,
            "namespace": namespace,
            "policy_id": policy_id,
            "version": int(version),
            "parent_policy_id": row[0],
            "rules": _load(row[1], {}),
            "effective_rules": rules,
            "status": row[2],
            "idempotent": idempotent,
        }

    @coordinated
    def register_object(
        self,
        namespace,
        object_id,
        object_class,
        policy_id,
        policy_version,
        payload,
        *,
        created_at_ms,
        source_license=None,
        access_class="public",
        value_score=0.0,
        generation=0,
        dependencies=(),
        pins=(),
        principal_id,
        scopes,
    ):
        _require(scopes, ADMIN_SCOPE)
        self.policy(namespace, policy_id, policy_version, scopes={READ_SCOPE})
        row = self.conn.execute(
            "SELECT payload_json FROM retention_objects WHERE namespace=? AND object_id=?",
            [namespace, object_id],
        ).fetchone()
        if row:
            if _load(row[0], {}) != dict(payload):
                raise KnowledgeRetentionError(
                    "object_conflict", "retention object identity is immutable"
                )
            return {"namespace": namespace, "object_id": object_id, "idempotent": True}
        self.conn.execute(
            "INSERT INTO retention_objects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                object_id,
                object_class,
                source_license,
                access_class,
                created_at_ms,
                value_score,
                generation,
                policy_id,
                policy_version,
                _canon(dict(payload)),
                _canon(sorted(set(dependencies))),
                _canon(sorted(set(pins))),
                "active",
            ],
        )
        self._audit(namespace, "register_object", object_id, principal_id)
        return {
            "namespace": namespace,
            "object_id": object_id,
            "generation": generation,
            "idempotent": False,
        }

    @coordinated
    def place_hold(
        self, namespace, object_id, reason, *, principal_id, scopes, expires_at_ms=None
    ):
        _require(scopes, ADMIN_SCOPE)
        hold_id = (
            "legal-hold:" + _hash([namespace, object_id, reason, expires_at_ms])[:24]
        )
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO retention_holds VALUES (?,?,?,?,?,?,?)",
            [hold_id, namespace, object_id, reason, expires_at_ms, "active", now],
        )
        self._audit(namespace, "place_hold", hold_id, principal_id)
        return {
            "namespace": namespace,
            "hold_id": hold_id,
            "object_id": object_id,
            "reason": reason,
            "expires_at_ms": expires_at_ms,
            "status": "active",
        }

    @coordinated
    def release_hold(self, namespace, hold_id, *, principal_id, scopes):
        _require(scopes, ADMIN_SCOPE)
        row = self.conn.execute(
            "SELECT object_id FROM retention_holds WHERE namespace=? AND hold_id=?",
            [namespace, hold_id],
        ).fetchone()
        if not row:
            raise KnowledgeRetentionError("hold_not_found", "legal hold not found")
        self.conn.execute(
            "UPDATE retention_holds SET status='released' WHERE namespace=? AND hold_id=?",
            [namespace, hold_id],
        )
        self._audit(namespace, "release_hold", hold_id, principal_id)
        return {
            "namespace": namespace,
            "hold_id": hold_id,
            "object_id": row[0],
            "status": "released",
        }

    def explain(self, namespace, object_id, *, scopes):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT object_class,source_license,access_class,created_at_ms,value_score,generation,policy_id,policy_version,dependencies_json,pins_json,status FROM retention_objects WHERE namespace=? AND object_id=?",
            [namespace, object_id],
        ).fetchone()
        if not row:
            raise KnowledgeRetentionError(
                "object_not_found", "retention object not found"
            )
        policy = self.policy(namespace, row[6], row[7], scopes={READ_SCOPE})
        rules = policy["effective_rules"]
        reasons = []
        if self.now() - int(row[3]) < int(rules.get("minimum_age_ms", 0)):
            reasons.append("minimum_age")
        if rules.get("object_classes") and row[0] not in rules["object_classes"]:
            reasons.append("object_class")
        if rules.get("licenses") and row[1] not in rules["licenses"]:
            reasons.append("source_license")
        if rules.get("access_classes") and row[2] not in rules["access_classes"]:
            reasons.append("access_class")
        if float(row[4]) > float(rules.get("maximum_value_score", 1.0)):
            reasons.append("high_value")
        pins = _load(row[9], [])
        reasons.extend(f"pinned:{pin}" for pin in pins)
        holds = self.conn.execute(
            "SELECT hold_id,expires_at_ms FROM retention_holds WHERE namespace=? AND object_id=? AND status='active'",
            [namespace, object_id],
        ).fetchall()
        active_holds = [h[0] for h in holds if h[1] is None or int(h[1]) > self.now()]
        if active_holds:
            reasons.append("legal_hold")
        if row[10] != "active":
            reasons.append("not_active")
        from src.kb.managed_retention import guards
        payload = self.conn.execute('SELECT payload_json FROM retention_objects WHERE namespace=? AND object_id=?',[namespace,object_id]).fetchone()[0]
        reasons.extend(guards(self.conn,namespace,_load(payload,{}),self.now()))
        return {
            "namespace": namespace,
            "object_id": object_id,
            "eligible": not reasons,
            "reason_codes": reasons,
            "active_holds": active_holds,
            "pins": pins,
            "dependencies": _load(row[8], []),
            "policy_id": row[6],
            "policy_version": int(row[7]),
            "generation": int(row[5]),
            "dry_run": True,
        }

    def checkpoint(
        self,
        namespace,
        generation_start,
        generation_end,
        records,
        *,
        schema_version,
        tombstones=(),
        principal_id,
        scopes,
        cancel_requested=False,
        limit=1000,
    ):
        _require(scopes, EXECUTE_SCOPE)
        cap = _bound(limit)
        bounded = list(islice(iter(records), cap + 1))
        if len(bounded) > cap:
            raise KnowledgeRetentionError(
                "checkpoint_too_large",
                "checkpoint exceeds record limit; split the generation range explicitly",
                limit=cap,
            )
        tombstones = list(islice(iter(tombstones), cap + 1))
        if len(tombstones) > cap:
            raise KnowledgeRetentionError(
                "checkpoint_too_large", "checkpoint exceeds tombstone limit", limit=cap
            )
        content = {
            "schema_version": schema_version,
            "generation_start": generation_start,
            "generation_end": generation_end,
            "records": bounded,
            "tombstones": list(tombstones),
        }
        digest = _hash(content)
        checkpoint_id = "retention-checkpoint:" + _hash([namespace, digest])[:24]
        status = "cancelled" if cancel_requested else "complete"
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO retention_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                checkpoint_id,
                namespace,
                generation_start,
                generation_end,
                schema_version,
                digest,
                _canon(bounded),
                _canon(list(tombstones)),
                status,
                now,
            ],
        )
        self._audit(
            namespace, "checkpoint", checkpoint_id, principal_id, {"status": status}
        )
        return {
            "contract": CHECKPOINT_CONTRACT,
            "checkpoint_id": checkpoint_id,
            "namespace": namespace,
            "generation_start": generation_start,
            "generation_end": generation_end,
            "schema_version": schema_version,
            "content_hash": digest,
            "record_count": len(bounded),
            "tombstone_count": len(tombstones),
            "status": status,
        }

    def verify_checkpoint(
        self, namespace, checkpoint_id, *, scopes, records=None, tombstones=None
    ):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT generation_start,generation_end,schema_version,content_hash,records_json,tombstones_json,status FROM retention_checkpoints WHERE namespace=? AND checkpoint_id=?",
            [namespace, checkpoint_id],
        ).fetchone()
        if not row:
            raise KnowledgeRetentionError(
                "checkpoint_not_found", "checkpoint not found"
            )
        actual = _hash(
            {
                "schema_version": row[2],
                "generation_start": int(row[0]),
                "generation_end": int(row[1]),
                "records": _load(row[4], []) if records is None else records,
                "tombstones": _load(row[5], []) if tombstones is None else tombstones,
            }
        )
        return {
            "contract": CHECKPOINT_CONTRACT,
            "checkpoint_id": checkpoint_id,
            "namespace": namespace,
            "generation_start": int(row[0]),
            "generation_end": int(row[1]),
            "schema_version": row[2],
            "content_hash": row[3],
            "record_count": len(_load(row[4], [])),
            "tombstone_count": len(_load(row[5], [])),
            "status": row[6],
            "verified": actual == row[3],
            "actual_hash": actual,
        }

    def archive(
        self,
        namespace,
        checkpoint_id,
        storage,
        *,
        encryption=None,
        storage_available=True,
        partial=False,
        cancel_requested=False,
        principal_id,
        scopes,
    ):
        _require(scopes, EXECUTE_SCOPE)
        from src.kb.archive_io import archive_checkpoint
        return archive_checkpoint(self, namespace, checkpoint_id, storage,
            encryption=encryption, storage_available=storage_available,
            partial=partial, cancel_requested=cancel_requested, principal_id=principal_id)

    def restore(
        self, namespace, archive_id, *, principal_id, scopes, storage_available=True,
        manifest=None, supported_schema_versions=("1", "2", "3")
    ):
        _require(scopes, EXECUTE_SCOPE)
        from src.kb.archive_io import restore_archive
        return restore_archive(self, namespace, archive_id, principal_id=principal_id,
            storage_available=storage_available, manifest=manifest,
            supported_schema_versions=supported_schema_versions)

    def plan_gc(self, namespace, object_ids, *, principal_id, scopes, limit=1000):
        _require(scopes, ADMIN_SCOPE)
        candidates = list(dict.fromkeys(object_ids))[: _bound(limit)]
        eligible, blocked = [], {}
        all_rows = self.conn.execute(
            "SELECT object_id,dependencies_json,status FROM retention_objects WHERE namespace=?",
            [namespace],
        ).fetchall()
        reverse = {}
        for owner, dependencies, status in all_rows:
            if status == "active":
                for dependency in _load(dependencies, []):
                    reverse.setdefault(dependency, []).append(owner)
        candidate_set = set(candidates)
        for object_id in candidates:
            explanation = self.explain(namespace, object_id, scopes={READ_SCOPE})
            external = sorted(set(reverse.get(object_id, [])) - candidate_set)
            reasons = list(explanation["reason_codes"])
            if external:
                reasons.append("reachable_from_retained_object")
            if reasons:
                blocked[object_id] = {"reason_codes": reasons, "dependents": external}
            else:
                eligible.append(object_id)
        # A candidate blocked by a hold/pin remains a live dependent. Do not
        # reclaim its dependencies just because both appeared in the request.
        while True:
            newly_blocked = {oid: sorted(set(reverse.get(oid, [])) - set(eligible)) for oid in eligible}
            newly_blocked = {oid: owners for oid, owners in newly_blocked.items() if owners}
            if not newly_blocked:
                break
            for oid, owners in newly_blocked.items():
                eligible.remove(oid)
                blocked[oid] = {'reason_codes':['reachable_from_retained_object'],'dependents':owners}
        snapshot = {
            object_id: self._object_guard(namespace, object_id)
            for object_id in candidates
        }
        plan_id = (
            "retention-gc-plan:"
            + _hash([namespace, candidates, eligible, blocked, snapshot])[:24]
        )
        result = {
            "contract": PLAN_CONTRACT,
            "plan_id": plan_id,
            "namespace": namespace,
            "candidates": candidates,
            "eligible": eligible,
            "blocked": blocked,
            "guard_hashes": snapshot,
            "dry_run": True,
            "bounded": len(candidates) < len(object_ids),
        }
        self._audit(
            namespace, "plan_gc", plan_id, principal_id, {"eligible": len(eligible)}
        )
        return result

    def _object_guard(self, namespace, object_id):
        row = self.conn.execute(
            "SELECT pins_json,status FROM retention_objects WHERE namespace=? AND object_id=?",
            [namespace, object_id],
        ).fetchone()
        holds = self.conn.execute(
            "SELECT hold_id,status,expires_at_ms FROM retention_holds WHERE namespace=? AND object_id=? ORDER BY hold_id",
            [namespace, object_id],
        ).fetchall()
        payload=self.conn.execute('SELECT payload_json FROM retention_objects WHERE namespace=? AND object_id=?',[namespace,object_id]).fetchone()
        from src.kb.managed_retention import guards
        managed=guards(self.conn,namespace,_load(payload[0],{}) if payload else {},self.now())
        return _hash([_load(row[0], []) if row else [], row[1] if row else None, holds,managed])

    @coordinated
    def execute_gc(self, namespace, plan, *, principal_id, scopes,
                   cancel_requested=False, deletion_outcome="success"):
        self.conn.execute('BEGIN TRANSACTION')
        try:
            result=self._execute_gc(namespace,plan,principal_id=principal_id,scopes=scopes,
                cancel_requested=cancel_requested,deletion_outcome=deletion_outcome)
            self.conn.execute('COMMIT')
            return result
        except Exception:
            self.conn.execute('ROLLBACK')
            raise

    def _execute_gc(
        self,
        namespace,
        plan,
        *,
        principal_id,
        scopes,
        cancel_requested=False,
        deletion_outcome="success",
    ):
        _require(scopes, EXECUTE_SCOPE)
        plan_id = plan["plan_id"]
        input_hash = _hash(plan)
        job_id = "retention-job:" + _hash([namespace, "gc", input_hash])[:24]
        prior = self.conn.execute(
            "SELECT result_json FROM retention_jobs WHERE namespace=? AND job_id=?",
            [namespace, job_id],
        ).fetchone()
        if prior and _load(prior[0], {}).get("status") != "failed":
            return {**_load(prior[0], {}), "idempotent": True}
        if plan.get('namespace') != namespace:
            raise KnowledgeRetentionError('invalid_plan','plan belongs to another namespace')
        fresh=self.plan_gc(namespace,plan['candidates'],principal_id=principal_id,scopes={ADMIN_SCOPE})
        if any(plan.get(key)!=fresh[key] for key in ('plan_id','eligible','blocked','guard_hashes')):
            raise KnowledgeRetentionError('stale_plan','retention policy, reachability, pins or holds changed after planning')
        stale = [
            object_id
            for object_id, guard in plan["guard_hashes"].items()
            if self._object_guard(namespace, object_id) != guard
        ]
        if stale:
            raise KnowledgeRetentionError(
                "stale_plan", "pins or holds changed after planning", object_ids=stale
            )
        status = (
            "cancelled"
            if cancel_requested
            else "failed"
            if deletion_outcome != "success"
            else "completed"
        )
        deleted = []
        if status == "completed":
            for object_id in plan["eligible"]:
                from src.kb.managed_retention import reclaim
                payload=self.conn.execute('SELECT payload_json FROM retention_objects WHERE namespace=? AND object_id=?',[namespace,object_id]).fetchone()[0]
                reclaim(self.conn,namespace,object_id,_load(payload,{}),self.now())
                self.conn.execute(
                    "UPDATE retention_objects SET status='tombstoned' WHERE namespace=? AND object_id=? AND status='active'",
                    [namespace, object_id],
                )
                deleted.append(object_id)
        result = {
            "contract": JOB_CONTRACT,
            "job_id": job_id,
            "namespace": namespace,
            "job_type": "gc",
            "plan_id": plan_id,
            "status": status,
            "processed": len(deleted),
            "tombstoned": deleted,
            "input_hash": input_hash,
        }
        now = self.now()
        if prior:
            self.conn.execute(
                "UPDATE retention_jobs SET status=?,processed=?,result_json=?,updated_at_ms=? WHERE namespace=? AND job_id=?",
                [status, len(deleted), _canon(result), now, namespace, job_id],
            )
        else:
            self.conn.execute(
                "INSERT INTO retention_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    job_id,
                    namespace,
                    "gc",
                    input_hash,
                    status,
                    len(deleted),
                    _canon(result),
                    now,
                    now,
                ],
            )
        self._audit(namespace, "execute_gc", job_id, principal_id, {"status": status})
        return {**result, "idempotent": False}

    def job(self, namespace, job_id, *, scopes):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT result_json FROM retention_jobs WHERE namespace=? AND job_id=?",
            [namespace, job_id],
        ).fetchone()
        if not row:
            raise KnowledgeRetentionError("job_not_found", "retention job not found")
        return _load(row[0], {})

    def cancel_job(self, namespace, job_id, *, principal_id, scopes):
        _require(scopes, EXECUTE_SCOPE)
        result = self.job(namespace, job_id, scopes={READ_SCOPE})
        if result["status"] in {"completed", "failed", "cancelled"}:
            return {**result, "cancellable": False}
        result["status"] = "cancelled"
        self.conn.execute(
            "UPDATE retention_jobs SET status='cancelled',result_json=?,updated_at_ms=? WHERE namespace=? AND job_id=?",
            [_canon(result), self.now(), namespace, job_id],
        )
        self._audit(namespace, "cancel_job", job_id, principal_id)
        return {**result, "cancellable": True}

    def health(self, namespace, *, scopes):
        _require(scopes, READ_SCOPE)
        active = self.conn.execute(
            "SELECT count(*) FROM retention_objects WHERE namespace=? AND status='active'",
            [namespace],
        ).fetchone()[0]
        archived = self.conn.execute(
            "SELECT count(*) FROM retention_archives WHERE namespace=? AND status IN ('archived','restored')",
            [namespace],
        ).fetchone()[0]
        failures = self.conn.execute(
            "SELECT count(*) FROM retention_jobs WHERE namespace=? AND status='failed'",
            [namespace],
        ).fetchone()[0]
        held = self.conn.execute(
            "SELECT count(*) FROM retention_holds WHERE namespace=? AND status='active' AND (expires_at_ms IS NULL OR expires_at_ms>?)",
            [namespace, self.now()],
        ).fetchone()[0]
        return {
            "namespace": namespace,
            "status": "degraded" if failures else "healthy",
            "active_objects": int(active),
            "archived_checkpoints": int(archived),
            "active_holds": int(held),
            "failed_jobs": int(failures),
        }
