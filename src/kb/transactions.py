"""Atomic, provenance-native knowledge mutations with preview-bound approval.

The transaction store is an overlay for agent/user-authored knowledge. Raw
source documents remain immutable; assertions and corrections are recorded in
separate versioned object/relation rows with explicit provenance and evidence.
The same namespace key supports the shared ``corpus`` and provisioned KGs.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

CONTRACT = "noesis-knowledge-mutation-v1"
PREVIEW_CONTRACT = "noesis-knowledge-mutation-preview-v1"
RESULT_CONTRACT = "noesis-knowledge-mutation-result-v1"
AUDIT_CONTRACT = "noesis-knowledge-mutation-audit-v1"
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts/schemas/jsonschema/noesis-knowledge-mutation-v1.json"
)

PREVIEW_SCOPE = "knowledge:transaction:preview"
COMMIT_SCOPE = "knowledge:transaction:commit"
ROLLBACK_SCOPE = "knowledge:transaction:rollback"
READ_SCOPE = "knowledge:transaction:read"

_WRITE_LOCK = threading.RLock()

_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_objects (
    namespace TEXT NOT NULL,
    object_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    value_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    provenance_kind TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    revision BIGINT NOT NULL,
    retracted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    last_batch_id TEXT NOT NULL,
    PRIMARY KEY (namespace, object_id)
);
CREATE TABLE IF NOT EXISTS knowledge_relations (
    namespace TEXT NOT NULL,
    relation_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    provenance_kind TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    revision BIGINT NOT NULL,
    retracted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    last_batch_id TEXT NOT NULL,
    PRIMARY KEY (namespace, relation_id)
);
CREATE TABLE IF NOT EXISTS knowledge_transaction_batches (
    batch_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    namespace TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    approval_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    compensation_json TEXT NOT NULL,
    rollback_result_json TEXT,
    status TEXT NOT NULL,
    committed_at_ms BIGINT NOT NULL,
    rolled_back_at_ms BIGINT
);
CREATE TABLE IF NOT EXISTS knowledge_transaction_audit (
    sequence BIGINT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    approved_preview_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    affected_json TEXT NOT NULL,
    created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_consolidation_watermarks (
    namespace TEXT PRIMARY KEY,
    watermark BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    last_batch_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_derivation_invalidations (
    batch_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    invalidated_at_ms BIGINT NOT NULL,
    PRIMARY KEY (batch_id, artifact_type, artifact_id)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_objects_type
    ON knowledge_objects(namespace, object_type, retracted);
CREATE INDEX IF NOT EXISTS idx_knowledge_relations_spo
    ON knowledge_relations(namespace, subject_id, predicate, object_id, retracted);
CREATE INDEX IF NOT EXISTS idx_knowledge_audit_batch
    ON knowledge_transaction_audit(batch_id, sequence);
"""


class TransactionError(RuntimeError):
    """Typed transaction failure safe to expose through MCP."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def ensure_transaction_schema(conn: Any) -> None:
    """Create the additive transaction tables and indexes idempotently."""

    conn.execute(_DDL)
    conn.execute(
        "ALTER TABLE knowledge_transaction_batches "
        "ADD COLUMN IF NOT EXISTS rollback_result_json TEXT"
    )


def _table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]
        ).fetchone()
    )


def _column_exists(conn: Any, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            [table, column],
        ).fetchone()
    )


def _row_object(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "kind": "object",
        "id": row[0],
        "object_type": row[1],
        "value": json.loads(row[2]),
        "metadata": json.loads(row[3]),
        "provenance_kind": row[4],
        "provenance": json.loads(row[5]),
        "evidence": json.loads(row[6]),
        "actor_id": row[7],
        "revision": int(row[8]),
        "retracted": bool(row[9]),
        "created_at_ms": int(row[10]),
        "updated_at_ms": int(row[11]),
        "last_batch_id": row[12],
    }


def _row_relation(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "kind": "relation",
        "id": row[0],
        "subject_id": row[1],
        "predicate": row[2],
        "object_id": row[3],
        "metadata": json.loads(row[4]),
        "provenance_kind": row[5],
        "provenance": json.loads(row[6]),
        "evidence": json.loads(row[7]),
        "actor_id": row[8],
        "revision": int(row[9]),
        "retracted": bool(row[10]),
        "created_at_ms": int(row[11]),
        "updated_at_ms": int(row[12]),
        "last_batch_id": row[13],
    }


class KnowledgeTransactionStore:
    """Preview, atomically commit, audit, and compensate mutation batches."""

    def __init__(
        self,
        conn: Any,
        *,
        clock: Callable[[], int] | None = None,
        failure_hook: Callable[[int, Mapping[str, Any]], None] | None = None,
        initialize: bool = True,
    ) -> None:
        self.conn = conn
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.failure_hook = failure_hook
        if initialize:
            ensure_transaction_schema(conn)

    @staticmethod
    def _require_scope(scopes: Iterable[str], required: str) -> None:
        granted = {str(scope) for scope in scopes}
        if required not in granted:
            raise TransactionError(
                "unauthorized",
                f"missing required scope {required}",
                required_scope=required,
            )

    def _authorize(
        self,
        envelope: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
        required: str,
    ) -> None:
        self._require_scope(scopes, required)
        actor = envelope.get("actor") or {}
        if str(actor.get("principal_id", "")) != str(principal_id):
            raise TransactionError(
                "actor_mismatch",
                "the authenticated principal does not match envelope.actor",
            )
        namespace = str(envelope.get("namespace", ""))
        if namespace != "corpus":
            access = "read" if required == PREVIEW_SCOPE else "write"
            self._require_scope(scopes, f"knowledge:namespace:{namespace}:{access}")

    def _validate(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        try:
            payload = json.loads(_canonical(dict(envelope)))
        except (TypeError, ValueError) as exc:
            raise TransactionError("invalid_envelope", str(exc)) from exc
        try:
            from jsonschema import Draft7Validator

            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            errors = sorted(Draft7Validator(schema).iter_errors(payload), key=str)
        except OSError as exc:
            raise TransactionError("schema_unavailable", str(exc)) from exc
        if errors:
            raise TransactionError(
                "invalid_envelope",
                "mutation envelope failed schema validation",
                validation_errors=[error.message for error in errors[:20]],
            )
        mutation_ids = [item["mutation_id"] for item in payload["mutations"]]
        if len(set(mutation_ids)) != len(mutation_ids):
            raise TransactionError(
                "invalid_envelope", "mutation_id values must be unique"
            )
        provenance_kind = payload["provenance"]["kind"]
        actor_kind = payload["actor"]["kind"]
        if provenance_kind == "user-assertion" and actor_kind != "user":
            raise TransactionError(
                "invalid_provenance", "user-assertion provenance requires a user actor"
            )
        if provenance_kind == "agent-assertion" and actor_kind != "agent":
            raise TransactionError(
                "invalid_provenance",
                "agent-assertion provenance requires an agent actor",
            )
        if provenance_kind == "source-derived" and actor_kind != "service":
            raise TransactionError(
                "invalid_provenance",
                "source-derived provenance requires a service actor",
            )
        return payload

    def _namespace_ready(self, namespace: str) -> bool:
        if namespace == "corpus":
            return True
        if not _table_exists(self.conn, "provisioned_kgs"):
            return False
        return bool(
            self.conn.execute(
                "SELECT 1 FROM provisioned_kgs WHERE name = ? AND status = 'deployed'",
                [namespace],
            ).fetchone()
        )

    def _namespace_ontology(self, namespace: str) -> dict[str, Any]:
        """Return an optional provisioned ontology without requiring old registries to migrate."""

        if namespace == "corpus" or not _table_exists(self.conn, "provisioned_kgs"):
            return {}
        if not _column_exists(self.conn, "provisioned_kgs", "ontology"):
            # Early provisioning registries did not have an ontology column.
            return {}
        row = self.conn.execute(
            "SELECT ontology FROM provisioned_kgs "
            "WHERE name = ? AND status = 'deployed'",
            [namespace],
        ).fetchone()
        if not row or not row[0]:
            return {}
        try:
            ontology = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except (TypeError, ValueError):
            return {}
        return dict(ontology) if isinstance(ontology, Mapping) else {}

    @staticmethod
    def _ontology_values(ontology: Mapping[str, Any], *keys: str) -> set[str]:
        for key in keys:
            raw = ontology.get(key)
            if isinstance(raw, Mapping):
                return {str(value) for value in raw}
            if isinstance(raw, list):
                values = set()
                for value in raw:
                    if isinstance(value, Mapping):
                        name = value.get("name") or value.get("id") or value.get("type")
                        if name:
                            values.add(str(name))
                    elif value is not None:
                        values.add(str(value))
                return values
        return set()

    def _get(self, namespace: str, kind: str, identifier: str) -> dict[str, Any] | None:
        if kind == "object":
            if not _table_exists(self.conn, "knowledge_objects"):
                return None
            row = self.conn.execute(
                "SELECT object_id, object_type, value_json, metadata_json, "
                "provenance_kind, provenance_json, evidence_json, actor_id, revision, "
                "retracted, created_at_ms, updated_at_ms, last_batch_id "
                "FROM knowledge_objects WHERE namespace = ? AND object_id = ?",
                [namespace, identifier],
            ).fetchone()
            return _row_object(row)
        if not _table_exists(self.conn, "knowledge_relations"):
            return None
        row = self.conn.execute(
            "SELECT relation_id, subject_id, predicate, object_id, metadata_json, "
            "provenance_kind, provenance_json, evidence_json, actor_id, revision, "
            "retracted, created_at_ms, updated_at_ms, last_batch_id "
            "FROM knowledge_relations WHERE namespace = ? AND relation_id = ?",
            [namespace, identifier],
        ).fetchone()
        return _row_relation(row)

    @staticmethod
    def _invalidation(
        kind: str, identifier: str, mutation_type: str
    ) -> list[dict[str, str]]:
        artifacts = ["consolidation"]
        artifacts.append("search-index" if kind == "object" else "graph-index")
        return [
            {
                "artifact_type": artifact,
                "artifact_id": identifier,
                "reason": f"knowledge-{mutation_type}",
            }
            for artifact in artifacts
        ]

    def _plan(self, envelope: dict[str, Any]) -> dict[str, Any]:
        namespace = envelope["namespace"]
        ontology = self._namespace_ontology(namespace)
        object_types = self._ontology_values(
            ontology, "object_types", "entity_types", "entities"
        )
        relation_types = self._ontology_values(
            ontology, "relation_types", "predicates", "relations"
        )
        creates: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        retractions: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        invalidations: list[dict[str, str]] = []
        simulated: dict[tuple[str, str], dict[str, Any] | None] = {}
        touched: set[tuple[str, str]] = set()

        def current(kind: str, identifier: str) -> dict[str, Any] | None:
            key = (kind, identifier)
            if key not in simulated:
                simulated[key] = self._get(namespace, kind, identifier)
            return simulated[key]

        if not self._namespace_ready(namespace):
            conflicts.append({"code": "namespace_not_found", "namespace": namespace})

        for mutation in envelope["mutations"]:
            target = mutation["target"]
            kind, identifier = target["kind"], target["id"]
            expected = int(target["expected_revision"])
            existing = current(kind, identifier)
            mtype = mutation["type"]
            conflict: dict[str, Any] | None = None

            if (kind, identifier) in touched:
                conflict = {
                    "code": "duplicate_target",
                    "message": "a batch may mutate each target at most once",
                }
            touched.add((kind, identifier))

            if conflict:
                pass
            elif mtype == "assert":
                if kind != "object":
                    conflict = {
                        "code": "target_kind",
                        "message": "assert targets objects",
                    }
                elif existing is not None:
                    conflict = {
                        "code": "already_exists",
                        "actual_revision": existing["revision"],
                    }
                elif expected != 0:
                    conflict = {"code": "revision_conflict", "actual_revision": 0}
                elif object_types and mutation["object_type"] not in object_types:
                    conflict = {
                        "code": "ontology_violation",
                        "field": "object_type",
                        "value": mutation["object_type"],
                        "allowed": sorted(object_types),
                    }
                else:
                    after = {
                        "kind": "object",
                        "id": identifier,
                        "object_type": mutation["object_type"],
                        "value": mutation["value"],
                        "metadata": mutation.get("metadata", {}),
                        "revision": 1,
                        "retracted": False,
                    }
                    creates.append(
                        {"mutation_id": mutation["mutation_id"], "after": after}
                    )
                    simulated[(kind, identifier)] = after
            elif mtype == "link":
                relation = mutation.get("relation") or {}
                if kind != "relation":
                    conflict = {
                        "code": "target_kind",
                        "message": "link targets relations",
                    }
                elif existing is not None:
                    conflict = {
                        "code": "already_exists",
                        "actual_revision": existing["revision"],
                    }
                elif expected != 0:
                    conflict = {"code": "revision_conflict", "actual_revision": 0}
                elif relation_types and relation.get("predicate") not in relation_types:
                    conflict = {
                        "code": "ontology_violation",
                        "field": "predicate",
                        "value": relation.get("predicate"),
                        "allowed": sorted(relation_types),
                    }
                else:
                    missing = [
                        endpoint
                        for endpoint in (
                            relation.get("subject_id"),
                            relation.get("object_id"),
                        )
                        if not endpoint
                        or current("object", str(endpoint)) is None
                        or bool(current("object", str(endpoint))["retracted"])
                    ]
                    if missing:
                        conflict = {"code": "missing_endpoint", "object_ids": missing}
                    else:
                        after = {
                            "kind": "relation",
                            "id": identifier,
                            "subject_id": relation["subject_id"],
                            "predicate": relation["predicate"],
                            "object_id": relation["object_id"],
                            "metadata": relation.get("metadata", {}),
                            "revision": 1,
                            "retracted": False,
                        }
                        links.append(
                            {"mutation_id": mutation["mutation_id"], "after": after}
                        )
                        simulated[(kind, identifier)] = after
            else:
                if existing is None:
                    conflict = {"code": "not_found", "actual_revision": 0}
                elif existing["revision"] != expected:
                    conflict = {
                        "code": "revision_conflict",
                        "expected_revision": expected,
                        "actual_revision": existing["revision"],
                    }
                elif mtype == "correct":
                    if kind != "object":
                        conflict = {
                            "code": "target_kind",
                            "message": "correct targets objects",
                        }
                    else:
                        after = {
                            **existing,
                            "value": mutation["value"],
                            "revision": expected + 1,
                        }
                        if "metadata" in mutation:
                            after["metadata"] = {
                                **existing["metadata"],
                                **mutation["metadata"],
                            }
                        after["retracted"] = False
                        updates.append(
                            {
                                "mutation_id": mutation["mutation_id"],
                                "before": existing,
                                "after": after,
                            }
                        )
                        simulated[(kind, identifier)] = after
                elif mtype == "set_metadata":
                    after = {
                        **existing,
                        "metadata": {**existing["metadata"], **mutation["metadata"]},
                        "revision": expected + 1,
                    }
                    updates.append(
                        {
                            "mutation_id": mutation["mutation_id"],
                            "before": existing,
                            "after": after,
                        }
                    )
                    simulated[(kind, identifier)] = after
                elif mtype == "retract":
                    if existing["retracted"]:
                        warnings.append(
                            {
                                "code": "already_retracted",
                                "kind": kind,
                                "id": identifier,
                            }
                        )
                    after = {**existing, "retracted": True, "revision": expected + 1}
                    retractions.append(
                        {
                            "mutation_id": mutation["mutation_id"],
                            "before": existing,
                            "after": after,
                        }
                    )
                    simulated[(kind, identifier)] = after

            if conflict:
                conflicts.append(
                    {
                        "mutation_id": mutation["mutation_id"],
                        "kind": kind,
                        "id": identifier,
                        **conflict,
                    }
                )
            else:
                invalidations.extend(self._invalidation(kind, identifier, mtype))

        invalidations = sorted(
            {_canonical(item): item for item in invalidations}.values(),
            key=lambda item: (
                item["artifact_type"],
                item["artifact_id"],
                item["reason"],
            ),
        )
        return {
            "creates": sorted(creates, key=lambda item: item["mutation_id"]),
            "updates": sorted(updates, key=lambda item: item["mutation_id"]),
            "links": sorted(links, key=lambda item: item["mutation_id"]),
            "retractions": sorted(retractions, key=lambda item: item["mutation_id"]),
            "conflicts": sorted(
                conflicts, key=lambda item: (item.get("mutation_id", ""), item["code"])
            ),
            "warnings": sorted(warnings, key=_canonical),
            "downstream_invalidations": invalidations,
        }

    def preview(
        self,
        envelope: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Validate and deterministically diff a batch without writing state."""

        payload = self._validate(envelope)
        self._authorize(payload, principal_id, scopes, PREVIEW_SCOPE)
        plan = self._plan(payload)
        base_watermark = self.watermark(payload["namespace"])
        approval_hash = _digest(
            {"envelope": payload, "base_watermark": base_watermark, "plan": plan}
        )
        return {
            "contract": PREVIEW_CONTRACT,
            "batch_id": payload["batch_id"],
            "namespace": payload["namespace"],
            "valid": not plan["conflicts"],
            "approval_hash": approval_hash,
            "base_watermark": base_watermark,
            **plan,
        }

    def watermark(self, namespace: str) -> int:
        if not _table_exists(self.conn, "knowledge_consolidation_watermarks"):
            return 0
        row = self.conn.execute(
            "SELECT watermark FROM knowledge_consolidation_watermarks WHERE namespace = ?",
            [namespace],
        ).fetchone()
        return int(row[0]) if row else 0

    def _write_state(
        self,
        namespace: str,
        state: Mapping[str, Any],
        envelope: Mapping[str, Any],
        now: int,
    ) -> None:
        provenance = _canonical(envelope["provenance"])
        evidence = _canonical(envelope["evidence"])
        actor = envelope["actor"]["principal_id"]
        batch_id = envelope["batch_id"]
        if state["kind"] == "object":
            prior = self._get(namespace, "object", state["id"])
            created = prior["created_at_ms"] if prior else now
            self.conn.execute(
                "INSERT OR REPLACE INTO knowledge_objects VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    namespace,
                    state["id"],
                    state["object_type"],
                    _canonical(state["value"]),
                    _canonical(state.get("metadata", {})),
                    envelope["provenance"]["kind"],
                    provenance,
                    evidence,
                    actor,
                    state["revision"],
                    state["retracted"],
                    created,
                    now,
                    batch_id,
                ],
            )
        else:
            prior = self._get(namespace, "relation", state["id"])
            created = prior["created_at_ms"] if prior else now
            self.conn.execute(
                "INSERT OR REPLACE INTO knowledge_relations VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    namespace,
                    state["id"],
                    state["subject_id"],
                    state["predicate"],
                    state["object_id"],
                    _canonical(state.get("metadata", {})),
                    envelope["provenance"]["kind"],
                    provenance,
                    evidence,
                    actor,
                    state["revision"],
                    state["retracted"],
                    created,
                    now,
                    batch_id,
                ],
            )

    def _append_audit(
        self,
        *,
        batch_id: str,
        action: str,
        actor: str,
        approval_hash: str,
        request: Any,
        result: Any,
        affected: list[dict[str, Any]],
        now: int,
    ) -> None:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM knowledge_transaction_audit"
        ).fetchone()
        sequence = int(row[0])
        event_id = f"audit:{sequence}:{_digest([batch_id, action, now])[:16]}"
        self.conn.execute(
            "INSERT INTO knowledge_transaction_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                sequence,
                event_id,
                batch_id,
                action,
                actor,
                approval_hash,
                _canonical(request),
                _canonical(result),
                _canonical(affected),
                now,
            ],
        )

    def commit(
        self,
        envelope: Mapping[str, Any],
        approval_hash: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Atomically commit an approved preview, or raise a typed conflict."""

        payload = self._validate(envelope)
        self._authorize(payload, principal_id, scopes, COMMIT_SCOPE)
        ensure_transaction_schema(self.conn)
        request_hash = _digest(payload)
        with _WRITE_LOCK:
            existing_batch = self.conn.execute(
                "SELECT idempotency_key, request_hash FROM knowledge_transaction_batches "
                "WHERE batch_id = ?",
                [payload["batch_id"]],
            ).fetchone()
            if existing_batch and existing_batch[0] != payload["idempotency_key"]:
                raise TransactionError(
                    "batch_id_reused",
                    "batch_id is already bound to a different idempotency key",
                )
            existing = self.conn.execute(
                "SELECT request_hash, result_json FROM knowledge_transaction_batches "
                "WHERE idempotency_key = ?",
                [payload["idempotency_key"]],
            ).fetchone()
            if existing:
                if existing[0] != request_hash:
                    raise TransactionError(
                        "idempotency_key_reused",
                        "idempotency key is already bound to different content",
                    )
                result = json.loads(existing[1])
                return {**result, "idempotent_replay": True}

            self.conn.execute("BEGIN TRANSACTION")
            try:
                # Re-plan under the write lock/transaction. Any intervening
                # revision or watermark changes the approval hash.
                plan = self._plan(payload)
                base_watermark = self.watermark(payload["namespace"])
                actual_hash = _digest(
                    {
                        "envelope": payload,
                        "base_watermark": base_watermark,
                        "plan": plan,
                    }
                )
                if actual_hash != approval_hash:
                    raise TransactionError(
                        "stale_approval",
                        "approval hash does not match the current deterministic preview",
                        expected=actual_hash,
                        supplied=approval_hash,
                    )
                if plan["conflicts"]:
                    raise TransactionError(
                        "conflict",
                        "atomic batch has conflicts; no mutations were applied",
                        conflicts=plan["conflicts"],
                    )
                now = int(self.clock())
                changes = [
                    *plan["creates"],
                    *plan["updates"],
                    *plan["links"],
                    *plan["retractions"],
                ]
                affected = []
                compensations = []
                for index, change in enumerate(changes, start=1):
                    before = change.get("before")
                    after = change["after"]
                    self._write_state(payload["namespace"], after, payload, now)
                    affected.append(
                        {
                            "kind": after["kind"],
                            "id": after["id"],
                            "revision": after["revision"],
                        }
                    )
                    compensations.append(
                        {
                            "kind": after["kind"],
                            "id": after["id"],
                            "committed_revision": after["revision"],
                            "before": before,
                        }
                    )
                    if self.failure_hook:
                        self.failure_hook(index, change)

                new_watermark = base_watermark + 1
                self.conn.execute(
                    "INSERT OR REPLACE INTO knowledge_consolidation_watermarks "
                    "VALUES (?, ?, ?, ?)",
                    [payload["namespace"], new_watermark, now, payload["batch_id"]],
                )
                for invalidation in plan["downstream_invalidations"]:
                    self.conn.execute(
                        "INSERT INTO knowledge_derivation_invalidations VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            payload["batch_id"],
                            payload["namespace"],
                            invalidation["artifact_type"],
                            invalidation["artifact_id"],
                            invalidation["reason"],
                            now,
                        ],
                    )
                result = {
                    "contract": RESULT_CONTRACT,
                    "ok": True,
                    "batch_id": payload["batch_id"],
                    "namespace": payload["namespace"],
                    "approval_hash": approval_hash,
                    "watermark": new_watermark,
                    "affected": sorted(
                        affected, key=lambda item: (item["kind"], item["id"])
                    ),
                    "invalidations": plan["downstream_invalidations"],
                    "committed_at_ms": now,
                    "idempotent_replay": False,
                }
                self.conn.execute(
                    "INSERT INTO knowledge_transaction_batches "
                    "(batch_id, idempotency_key, namespace, principal_id, request_hash, "
                    "approval_hash, request_json, result_json, compensation_json, "
                    "rollback_result_json, status, committed_at_ms, rolled_back_at_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'committed', ?, NULL)",
                    [
                        payload["batch_id"],
                        payload["idempotency_key"],
                        payload["namespace"],
                        principal_id,
                        request_hash,
                        approval_hash,
                        _canonical(payload),
                        _canonical(result),
                        _canonical(compensations),
                        now,
                    ],
                )
                self._append_audit(
                    batch_id=payload["batch_id"],
                    action="commit",
                    actor=principal_id,
                    approval_hash=approval_hash,
                    request=payload,
                    result=result,
                    affected=result["affected"],
                    now=now,
                )
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def rollback(
        self,
        batch_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Apply a new compensating revision while preserving commit history."""

        self._require_scope(scopes, ROLLBACK_SCOPE)
        if not str(reason).strip():
            raise TransactionError("invalid_rollback", "rollback reason is required")
        ensure_transaction_schema(self.conn)
        with _WRITE_LOCK:
            row = self.conn.execute(
                "SELECT namespace, approval_hash, request_json, compensation_json, "
                "status, rolled_back_at_ms, rollback_result_json "
                "FROM knowledge_transaction_batches "
                "WHERE batch_id = ?",
                [batch_id],
            ).fetchone()
            if row is None:
                raise TransactionError("not_found", f"batch {batch_id!r} was not found")
            (
                namespace,
                approval_hash,
                request_json,
                compensation_json,
                status,
                _rolled_at,
                stored,
            ) = row
            self._require_scope(
                scopes, f"knowledge:namespace:{namespace}:write"
            ) if namespace != "corpus" else None
            if status == "rolled_back":
                previous = json.loads(stored)
                return {**previous, "idempotent_replay": True}
            compensations = json.loads(compensation_json)
            request = json.loads(request_json)
            conflicts = []
            for item in compensations:
                current = self._get(namespace, item["kind"], item["id"])
                if current is None or current["revision"] != item["committed_revision"]:
                    conflicts.append(
                        {
                            "kind": item["kind"],
                            "id": item["id"],
                            "expected_revision": item["committed_revision"],
                            "actual_revision": current["revision"] if current else 0,
                        }
                    )
            if conflicts:
                raise TransactionError(
                    "rollback_conflict",
                    "later revisions prevent safe compensation",
                    conflicts=conflicts,
                )

            self.conn.execute("BEGIN TRANSACTION")
            try:
                now = int(self.clock())
                affected = []
                rollback_envelope = {
                    **request,
                    "batch_id": f"rollback:{batch_id}",
                    "actor": {"principal_id": principal_id, "kind": "user"},
                    "reason": reason,
                    "provenance": {
                        "kind": "user-assertion",
                        "method": "compensating-rollback",
                    },
                }
                for item in compensations:
                    current = self._get(namespace, item["kind"], item["id"])
                    before = item["before"]
                    if before is None:
                        restored = {
                            **current,
                            "retracted": True,
                            "revision": current["revision"] + 1,
                        }
                    else:
                        restored = {
                            **before,
                            "revision": current["revision"] + 1,
                        }
                    self._write_state(namespace, restored, rollback_envelope, now)
                    affected.append(
                        {
                            "kind": restored["kind"],
                            "id": restored["id"],
                            "revision": restored["revision"],
                        }
                    )
                new_watermark = self.watermark(namespace) + 1
                rollback_batch_id = f"rollback:{batch_id}"
                self.conn.execute(
                    "INSERT OR REPLACE INTO knowledge_consolidation_watermarks VALUES (?, ?, ?, ?)",
                    [namespace, new_watermark, now, rollback_batch_id],
                )
                for item in affected:
                    for invalidation in self._invalidation(
                        item["kind"], item["id"], "rollback"
                    ):
                        self.conn.execute(
                            "INSERT INTO knowledge_derivation_invalidations VALUES (?, ?, ?, ?, ?, ?)",
                            [
                                rollback_batch_id,
                                namespace,
                                invalidation["artifact_type"],
                                invalidation["artifact_id"],
                                invalidation["reason"],
                                now,
                            ],
                        )
                result = {
                    "contract": RESULT_CONTRACT,
                    "ok": True,
                    "batch_id": batch_id,
                    "action": "rollback",
                    "reason": reason,
                    "watermark": new_watermark,
                    "affected": sorted(
                        affected, key=lambda item: (item["kind"], item["id"])
                    ),
                    "rolled_back_at_ms": now,
                    "idempotent_replay": False,
                }
                self.conn.execute(
                    "UPDATE knowledge_transaction_batches SET status='rolled_back', "
                    "rolled_back_at_ms=?, rollback_result_json=? WHERE batch_id=?",
                    [now, _canonical(result), batch_id],
                )
                self._append_audit(
                    batch_id=batch_id,
                    action="rollback",
                    actor=principal_id,
                    approval_hash=approval_hash,
                    request={"batch_id": batch_id, "reason": reason},
                    result=result,
                    affected=result["affected"],
                    now=now,
                )
                self.conn.execute("COMMIT")
                return result
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def audit(
        self,
        *,
        principal_id: str,
        scopes: Iterable[str],
        batch_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Replay the append-only mutation trail in stable sequence order."""

        self._require_scope(scopes, READ_SCOPE)
        if not _table_exists(self.conn, "knowledge_transaction_audit"):
            return {
                "contract": AUDIT_CONTRACT,
                "principal_id": principal_id,
                "events": [],
                "next_sequence": int(after_sequence),
            }
        params: list[Any] = [max(0, int(after_sequence))]
        clause = "sequence > ?"
        if batch_id:
            clause += " AND batch_id = ?"
            params.append(batch_id)
        params.append(max(1, min(int(limit), 500)))
        rows = self.conn.execute(
            "SELECT sequence, event_id, batch_id, action, actor_id, "
            "approved_preview_hash, request_json, result_json, affected_json, "
            f"created_at_ms FROM knowledge_transaction_audit WHERE {clause} "
            "ORDER BY sequence LIMIT ?",
            params,
        ).fetchall()
        events = [
            {
                "sequence": int(row[0]),
                "event_id": row[1],
                "batch_id": row[2],
                "action": row[3],
                "actor_id": row[4],
                "approved_preview_hash": row[5],
                "request": json.loads(row[6]),
                "result": json.loads(row[7]),
                "affected": json.loads(row[8]),
                "created_at_ms": int(row[9]),
            }
            for row in rows
        ]
        return {
            "contract": AUDIT_CONTRACT,
            "principal_id": principal_id,
            "events": events,
            "next_sequence": events[-1]["sequence"] if events else int(after_sequence),
        }


__all__ = [
    "AUDIT_CONTRACT",
    "COMMIT_SCOPE",
    "CONTRACT",
    "PREVIEW_CONTRACT",
    "PREVIEW_SCOPE",
    "READ_SCOPE",
    "RESULT_CONTRACT",
    "ROLLBACK_SCOPE",
    "KnowledgeTransactionStore",
    "TransactionError",
    "ensure_transaction_schema",
]
