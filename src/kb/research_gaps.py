"""Versioned research-gap discovery, lifecycle, ranking, and task planning."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

POLICY_CONTRACT = "noesis-research-gap-policy-v1"
COVERAGE_CONTRACT = "noesis-research-coverage-v1"
GAP_CONTRACT = "noesis-research-gap-v1"
TASK_CONTRACT = "noesis-research-gap-task-v1"
REPORT_CONTRACT = "noesis-research-gap-report-v1"
READ_SCOPE = "knowledge:gaps:read"
WRITE_SCOPE = "knowledge:gaps:write"
REVIEW_SCOPE = "knowledge:gaps:review"
OBJECT_KINDS = {"claim", "entity", "event", "time-range", "geography", "methodology"}
GAP_STATUSES = {"open", "in-progress", "resolved", "dismissed"}

_DDL = """
CREATE TABLE IF NOT EXISTS research_gap_policies (
  policy_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, semantic_version TEXT NOT NULL,
  thresholds_json TEXT NOT NULL, weights_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  status TEXT NOT NULL, supersedes_policy_id TEXT, generation BIGINT NOT NULL,
  valid_from_ms BIGINT, valid_to_ms BIGINT, observed_at_ms BIGINT NOT NULL,
  producer_json TEXT NOT NULL, policy_context_json TEXT NOT NULL,
  provenance_json TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace, semantic_version)
);
CREATE TABLE IF NOT EXISTS research_coverage_observations (
  observation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, object_kind TEXT NOT NULL,
  object_id TEXT NOT NULL, dimension_json TEXT NOT NULL, dimension_key TEXT NOT NULL,
  coverage_known BOOLEAN NOT NULL, supports_json TEXT NOT NULL, signals_json TEXT NOT NULL,
  generation BIGINT NOT NULL, valid_from_ms BIGINT, valid_to_ms BIGINT,
  observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL, policy_json TEXT NOT NULL,
  provenance_json TEXT NOT NULL, principal_id TEXT NOT NULL, input_hash TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,object_kind,object_id,dimension_key,input_hash)
);
CREATE TABLE IF NOT EXISTS research_coverage_current (
  namespace TEXT NOT NULL, object_kind TEXT NOT NULL, object_id TEXT NOT NULL,
  dimension_key TEXT NOT NULL, observation_id TEXT NOT NULL,
  PRIMARY KEY(namespace,object_kind,object_id,dimension_key)
);
CREATE TABLE IF NOT EXISTS research_gaps (
  gap_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, object_kind TEXT NOT NULL,
  object_id TEXT NOT NULL, dimension_key TEXT NOT NULL, gap_type TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,object_kind,object_id,dimension_key,gap_type)
);
CREATE TABLE IF NOT EXISTS research_gap_revisions (
  gap_revision_id TEXT PRIMARY KEY, gap_id TEXT NOT NULL, namespace TEXT NOT NULL,
  revision BIGINT NOT NULL, predecessor_revision_id TEXT, status TEXT NOT NULL,
  policy_id TEXT NOT NULL, observation_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  explanation_json TEXT NOT NULL, generation BIGINT NOT NULL,
  valid_from_ms BIGINT, valid_to_ms BIGINT, observed_at_ms BIGINT NOT NULL,
  producer_json TEXT NOT NULL, policy_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
  principal_id TEXT NOT NULL, content_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(gap_id,revision), UNIQUE(gap_id,content_hash)
);
CREATE TABLE IF NOT EXISTS research_gap_current (
  gap_id TEXT PRIMARY KEY, gap_revision_id TEXT NOT NULL, revision BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_gap_tasks (
  task_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, gap_id TEXT NOT NULL,
  policy_id TEXT NOT NULL, rank BIGINT NOT NULL, score DOUBLE NOT NULL,
  estimated_cost DOUBLE NOT NULL, suggestion_json TEXT NOT NULL,
  status TEXT NOT NULL, plan_hash TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,gap_id,policy_id,plan_hash)
);
CREATE TABLE IF NOT EXISTS research_gap_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
"""


class ResearchGapError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise ResearchGapError("unauthorized", f"missing required scope {required}")


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = int(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise ResearchGapError(
            "invalid_cursor", "research-gap cursor is invalid"
        ) from exc
    if value < 0:
        raise ResearchGapError("invalid_cursor", "research-gap cursor is invalid")
    return value


class ResearchGapStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(self, namespace, operation, object_id, principal_id, detail, now):
        audit_id = (
            "gap-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO research_gap_audit VALUES (?,?,?,?,?,?,?)",
            [
                audit_id,
                namespace,
                operation,
                object_id,
                principal_id,
                _canonical(detail),
                now,
            ],
        )

    def register_policy(
        self,
        namespace: str,
        semantic_version: str,
        thresholds: Mapping[str, Any],
        weights: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        supersedes_policy_id: str | None = None,
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy_context: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if not namespace.strip() or not semantic_version.strip():
            raise ResearchGapError(
                "invalid_policy", "namespace and version are required"
            )
        threshold_values = {
            "min_primary": 1,
            "min_independent": 2,
            "min_current": 1,
            "min_method_adequate": 1,
            **dict(thresholds),
        }
        for key in (
            "min_primary",
            "min_independent",
            "min_current",
            "min_method_adequate",
        ):
            if int(threshold_values[key]) < 0:
                raise ResearchGapError("invalid_policy", f"{key} must be non-negative")
        weight_values = {
            "decision_relevance": 0.3,
            "uncertainty_reduction": 0.25,
            "feasibility": 0.2,
            "freshness": 0.15,
            "policy_priority": 0.1,
            "cost": 0.1,
            **dict(weights),
        }
        if any(float(value) < 0 for value in weight_values.values()):
            raise ResearchGapError(
                "invalid_policy", "ranking weights must be non-negative"
            )
        now = self.now()
        stable = {
            "namespace": namespace,
            "semantic_version": semantic_version,
            "thresholds": threshold_values,
            "weights": weight_values,
            "supersedes_policy_id": supersedes_policy_id,
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
            "producer": dict(
                producer or {"name": "noesis-research-gaps", "version": "1.0.0"}
            ),
            "policy_context": dict(policy_context or {}),
            "provenance": dict(provenance or {}),
        }
        content_hash = _digest(
            {k: v for k, v in stable.items() if k != "observed_at_ms"}
        )
        policy_id = "research-gap-policy:" + _digest([namespace, semantic_version])[:24]
        existing = self.conn.execute(
            "SELECT content_hash FROM research_gap_policies WHERE policy_id=?",
            [policy_id],
        ).fetchone()
        if existing:
            if existing[0] != content_hash:
                raise ResearchGapError(
                    "immutable_version", "gap policy version has different content"
                )
            return {
                **self.policy(namespace, policy_id, scopes={READ_SCOPE}),
                "idempotent": True,
            }
        if supersedes_policy_id:
            old = self.policy(namespace, supersedes_policy_id, scopes={READ_SCOPE})
            if not old:
                raise ResearchGapError(
                    "policy_not_found", "superseded policy does not exist"
                )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO research_gap_policies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    policy_id,
                    namespace,
                    semantic_version,
                    _canonical(threshold_values),
                    _canonical(weight_values),
                    content_hash,
                    "active",
                    supersedes_policy_id,
                    generation,
                    valid_from_ms,
                    valid_to_ms,
                    stable["observed_at_ms"],
                    _canonical(stable["producer"]),
                    _canonical(stable["policy_context"]),
                    _canonical(stable["provenance"]),
                    principal_id,
                    now,
                ],
            )
            if supersedes_policy_id:
                self.conn.execute(
                    "UPDATE research_gap_policies SET status='superseded' WHERE policy_id=? AND namespace=?",
                    [supersedes_policy_id, namespace],
                )
            self._audit(namespace, "register-policy", policy_id, principal_id, {}, now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.policy(namespace, policy_id, scopes={READ_SCOPE})

    def policy(
        self, namespace: str, policy_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT semantic_version,thresholds_json,weights_json,content_hash,status,supersedes_policy_id,generation,valid_from_ms,valid_to_ms,observed_at_ms,producer_json,policy_context_json,provenance_json,principal_id,created_at_ms FROM research_gap_policies WHERE namespace=? AND policy_id=?",
            [namespace, policy_id],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": POLICY_CONTRACT,
            "policy_id": policy_id,
            "namespace": namespace,
            "semantic_version": row[0],
            "thresholds": _load(row[1], {}),
            "weights": _load(row[2], {}),
            "content_hash": row[3],
            "status": row[4],
            "supersedes_policy_id": row[5],
            "generation": int(row[6]),
            "valid_from_ms": row[7],
            "valid_to_ms": row[8],
            "observed_at_ms": int(row[9]),
            "producer": _load(row[10], {}),
            "policy": _load(row[11], {}),
            "provenance": _load(row[12], {}),
            "principal_id": row[13],
            "created_at_ms": int(row[14]),
        }

    def select_policy(
        self, namespace: str, *, scopes: set[str], semantic_version: str | None = None
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT policy_id FROM research_gap_policies WHERE namespace=? AND ((? IS NULL AND status='active') OR (? IS NOT NULL AND semantic_version=?)) ORDER BY observed_at_ms DESC,policy_id LIMIT 1",
            [namespace, semantic_version, semantic_version, semantic_version],
        ).fetchone()
        if not row:
            raise ResearchGapError("policy_not_found", "no research-gap policy exists")
        return self.policy(namespace, row[0], scopes=scopes)

    def observe(
        self,
        namespace: str,
        object_kind: str,
        object_id: str,
        dimension: Mapping[str, Any],
        *,
        coverage_known: bool,
        supports: Sequence[Mapping[str, Any]],
        signals: Mapping[str, Any],
        principal_id: str,
        scopes: set[str],
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if (
            object_kind not in OBJECT_KINDS
            or not object_id.strip()
            or not namespace.strip()
        ):
            raise ResearchGapError(
                "invalid_coverage",
                "valid namespace, object kind, and object ID are required",
            )
        if (
            valid_from_ms is not None
            and valid_to_ms is not None
            and valid_to_ms <= valid_from_ms
        ):
            raise ResearchGapError(
                "invalid_coverage", "valid_to_ms must follow valid_from_ms"
            )
        support_values = [dict(item) for item in supports]
        if coverage_known and any(
            not str(item.get("evidence_id", "")).strip() for item in support_values
        ):
            raise ResearchGapError(
                "invalid_coverage", "every support requires an evidence_id"
            )
        now = self.now()
        dimension_value = dict(dimension)
        dimension_key = _digest(dimension_value)
        stable = {
            "namespace": namespace,
            "object_kind": object_kind,
            "object_id": object_id,
            "dimension": dimension_value,
            "coverage_known": bool(coverage_known),
            "supports": support_values,
            "signals": dict(signals),
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
            "producer": dict(
                producer or {"name": "noesis-research-gaps", "version": "1.0.0"}
            ),
            "policy": dict(policy or {}),
            "provenance": dict(provenance or {}),
        }
        input_hash = _digest({k: v for k, v in stable.items() if k != "observed_at_ms"})
        observation_id = (
            "research-coverage:"
            + _digest([namespace, object_kind, object_id, dimension_key, input_hash])[
                :24
            ]
        )
        existing = self.conn.execute(
            "SELECT created_at_ms FROM research_coverage_observations WHERE observation_id=?",
            [observation_id],
        ).fetchone()
        if not existing:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    "INSERT INTO research_coverage_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        observation_id,
                        namespace,
                        object_kind,
                        object_id,
                        _canonical(dimension_value),
                        dimension_key,
                        bool(coverage_known),
                        _canonical(support_values),
                        _canonical(dict(signals)),
                        generation,
                        valid_from_ms,
                        valid_to_ms,
                        stable["observed_at_ms"],
                        _canonical(stable["producer"]),
                        _canonical(stable["policy"]),
                        _canonical(stable["provenance"]),
                        principal_id,
                        input_hash,
                        now,
                    ],
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO research_coverage_current VALUES (?,?,?,?,?)",
                    [namespace, object_kind, object_id, dimension_key, observation_id],
                )
                self._audit(namespace, "observe", observation_id, principal_id, {}, now)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {
            "contract": COVERAGE_CONTRACT,
            "observation_id": observation_id,
            **stable,
            "dimension_key": dimension_key,
            "input_hash": input_hash,
            "created_at_ms": int(existing[0]) if existing else now,
            "idempotent": bool(existing),
        }

    def _coverage(self, namespace: str, observation_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT object_kind,object_id,dimension_json,dimension_key,coverage_known,supports_json,signals_json,generation,valid_from_ms,valid_to_ms,observed_at_ms,producer_json,policy_json,provenance_json,input_hash FROM research_coverage_observations WHERE namespace=? AND observation_id=?",
            [namespace, observation_id],
        ).fetchone()
        if not row:
            raise ResearchGapError(
                "coverage_not_found", "coverage observation does not exist"
            )
        return {
            "observation_id": observation_id,
            "object_kind": row[0],
            "object_id": row[1],
            "dimension": _load(row[2], {}),
            "dimension_key": row[3],
            "coverage_known": bool(row[4]),
            "supports": _load(row[5], []),
            "signals": _load(row[6], {}),
            "generation": int(row[7]),
            "valid_from_ms": row[8],
            "valid_to_ms": row[9],
            "observed_at_ms": int(row[10]),
            "producer": _load(row[11], {}),
            "policy": _load(row[12], {}),
            "provenance": _load(row[13], {}),
            "input_hash": row[14],
        }

    @staticmethod
    def _has_citation_cycle(supports: Sequence[Mapping[str, Any]]) -> bool:
        graph = {
            str(item.get("source_id")): {
                str(value) for value in item.get("cites_source_ids", [])
            }
            for item in supports
            if item.get("source_id")
        }

        def visit(node: str, path: set[str]) -> bool:
            if node in path:
                return True
            return any(
                visit(next_node, path | {node})
                for next_node in graph.get(node, set())
                if next_node in graph
            )

        return any(visit(node, set()) for node in graph)

    @staticmethod
    def _conditions(
        coverage: Mapping[str, Any], policy: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        supports = list(coverage["supports"])
        thresholds = policy["thresholds"]
        if not coverage["coverage_known"]:
            return [
                {
                    "gap_type": "unknown-coverage",
                    "evidence": [],
                    "actual": None,
                    "required": "known",
                }
            ]
        accessible = [item for item in supports if item.get("accessible", True)]
        usable = [item for item in accessible if not item.get("retracted", False)]
        primary = [item for item in usable if item.get("primary", False)]
        independent = {
            item.get("independence_group") or item.get("source_id") for item in usable
        }
        current = [item for item in usable if item.get("current", False)]
        adequate = [item for item in usable if item.get("method_adequate", False)]
        conditions: list[dict[str, Any]] = []

        def shortfall(gap_type: str, actual: int, required: int, evidence=usable):
            if actual < required:
                conditions.append(
                    {
                        "gap_type": gap_type,
                        "actual": actual,
                        "required": required,
                        "evidence": evidence,
                    }
                )

        shortfall("missing-primary", len(primary), int(thresholds["min_primary"]))
        shortfall(
            "insufficient-independent-support",
            len(independent),
            int(thresholds["min_independent"]),
        )
        shortfall(
            "missing-current-support", len(current), int(thresholds["min_current"])
        )
        shortfall(
            "methodologically-inadequate",
            len(adequate),
            int(thresholds["min_method_adequate"]),
        )
        inaccessible = [item for item in supports if not item.get("accessible", True)]
        if inaccessible:
            conditions.append(
                {
                    "gap_type": "inaccessible-evidence",
                    "actual": len(inaccessible),
                    "required": 0,
                    "evidence": inaccessible,
                }
            )
        if any(item.get("retracted", False) for item in supports):
            conditions.append(
                {
                    "gap_type": "retracted-support",
                    "actual": sum(bool(item.get("retracted")) for item in supports),
                    "required": 0,
                    "evidence": [item for item in supports if item.get("retracted")],
                }
            )
        supporting = [
            item for item in usable if item.get("stance", "supports") == "supports"
        ]
        contradicting = [item for item in usable if item.get("stance") == "contradicts"]
        meaningful = []
        support_hashes = {
            item.get("content_hash") for item in supporting if item.get("content_hash")
        }
        support_sources = {
            item.get("source_id") for item in supporting if item.get("source_id")
        }
        for item in contradicting:
            mirrored = item.get("mirrored_from") in support_sources
            duplicate = (
                item.get("content_hash") and item.get("content_hash") in support_hashes
            )
            if not mirrored and not duplicate:
                meaningful.append(item)
        if supporting and meaningful:
            conditions.append(
                {
                    "gap_type": "unresolved-contradiction",
                    "actual": len(meaningful),
                    "required": 0,
                    "evidence": [*supporting, *meaningful],
                }
            )
        if (
            supports
            and not primary
            and any(item.get("cites_source_ids") for item in supports)
        ):
            conditions.append(
                {
                    "gap_type": "missing-original-source",
                    "actual": 0,
                    "required": 1,
                    "evidence": supports,
                }
            )
        if ResearchGapStore._has_citation_cycle(supports):
            conditions.append(
                {
                    "gap_type": "circular-citation",
                    "actual": 1,
                    "required": 0,
                    "evidence": supports,
                }
            )
        return conditions

    def _gap_value(self, namespace: str, gap_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT g.object_kind,g.object_id,g.dimension_key,g.gap_type,r.gap_revision_id,r.revision,r.predecessor_revision_id,r.status,r.policy_id,r.observation_id,r.detail_json,r.explanation_json,r.generation,r.valid_from_ms,r.valid_to_ms,r.observed_at_ms,r.producer_json,r.policy_json,r.provenance_json,r.principal_id,r.content_hash,r.created_at_ms FROM research_gaps g JOIN research_gap_current c ON c.gap_id=g.gap_id JOIN research_gap_revisions r ON r.gap_revision_id=c.gap_revision_id WHERE g.namespace=? AND g.gap_id=?",
            [namespace, gap_id],
        ).fetchone()
        if not row:
            return None
        coverage = self._coverage(namespace, row[9])
        return {
            "contract": GAP_CONTRACT,
            "gap_id": gap_id,
            "namespace": namespace,
            "object_kind": row[0],
            "object_id": row[1],
            "dimension_key": row[2],
            "dimension": coverage["dimension"],
            "gap_type": row[3],
            "gap_revision_id": row[4],
            "revision": int(row[5]),
            "predecessor_revision_id": row[6],
            "status": row[7],
            "policy_id": row[8],
            "observation_id": row[9],
            "detail": _load(row[10], {}),
            "explanation": _load(row[11], {}),
            "generation": int(row[12]),
            "valid_from_ms": row[13],
            "valid_to_ms": row[14],
            "observed_at_ms": int(row[15]),
            "producer": _load(row[16], {}),
            "policy": _load(row[17], {}),
            "provenance": _load(row[18], {}),
            "principal_id": row[19],
            "content_hash": row[20],
            "created_at_ms": int(row[21]),
        }

    def get(
        self, namespace: str, gap_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        return self._gap_value(namespace, gap_id)

    def _write_gap_revision(
        self,
        gap_id: str,
        namespace: str,
        status: str,
        policy_id: str,
        coverage: Mapping[str, Any],
        detail: Mapping[str, Any],
        explanation: Mapping[str, Any],
        *,
        principal_id: str,
        now: int,
    ) -> tuple[str, bool]:
        current = self._gap_value(namespace, gap_id)
        semantic = {
            "status": status,
            "policy_id": policy_id,
            "observation_id": coverage["observation_id"],
            "detail": dict(detail),
            "explanation": dict(explanation),
        }
        content_hash = _digest(semantic)
        if current and current["content_hash"] == content_hash:
            return current["gap_revision_id"], True
        revision = current["revision"] + 1 if current else 1
        predecessor = current["gap_revision_id"] if current else None
        revision_id = (
            "research-gap-revision:" + _digest([gap_id, revision, content_hash])[:24]
        )
        self.conn.execute(
            "INSERT INTO research_gap_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                revision_id,
                gap_id,
                namespace,
                revision,
                predecessor,
                status,
                policy_id,
                coverage["observation_id"],
                _canonical(dict(detail)),
                _canonical(dict(explanation)),
                coverage["generation"],
                coverage["valid_from_ms"],
                coverage["valid_to_ms"],
                coverage["observed_at_ms"],
                _canonical(coverage["producer"]),
                _canonical(coverage["policy"]),
                _canonical(coverage["provenance"]),
                principal_id,
                content_hash,
                now,
            ],
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO research_gap_current VALUES (?,?,?)",
            [gap_id, revision_id, revision],
        )
        return revision_id, False

    def discover(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: set[str],
        policy_version: str | None = None,
        object_kind: str | None = None,
        limit: int = 100,
        cancel_requested: bool = False,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if object_kind is not None and object_kind not in OBJECT_KINDS:
            raise ResearchGapError(
                "invalid_object_kind", "unsupported research object kind"
            )
        if cancel_requested:
            return {
                "contract": REPORT_CONTRACT,
                "namespace": namespace,
                "status": "cancelled",
                "policy_id": None,
                "items": [],
                "scanned": 0,
                "report_hash": _digest([namespace, "cancelled"]),
            }
        selected = self.select_policy(
            namespace, scopes={READ_SCOPE}, semantic_version=policy_version
        )
        rows = self.conn.execute(
            "SELECT c.observation_id FROM research_coverage_current c JOIN research_coverage_observations o ON o.observation_id=c.observation_id WHERE c.namespace=? AND (? IS NULL OR o.object_kind=?) ORDER BY o.object_kind,o.object_id,o.dimension_key LIMIT ?",
            [namespace, object_kind, object_kind, min(max(limit, 1), 1000)],
        ).fetchall()
        now = self.now()
        items = []
        mutated = False
        self.conn.execute("BEGIN")
        try:
            for row in rows:
                coverage = self._coverage(namespace, row[0])
                conditions = self._conditions(coverage, selected)
                detected_types = {item["gap_type"] for item in conditions}
                for condition in conditions:
                    gap_id = (
                        "research-gap:"
                        + _digest(
                            [
                                namespace,
                                coverage["object_kind"],
                                coverage["object_id"],
                                coverage["dimension_key"],
                                condition["gap_type"],
                            ]
                        )[:24]
                    )
                    if not self.conn.execute(
                        "SELECT 1 FROM research_gaps WHERE gap_id=?", [gap_id]
                    ).fetchone():
                        self.conn.execute(
                            "INSERT INTO research_gaps VALUES (?,?,?,?,?,?,?)",
                            [
                                gap_id,
                                namespace,
                                coverage["object_kind"],
                                coverage["object_id"],
                                coverage["dimension_key"],
                                condition["gap_type"],
                                now,
                            ],
                        )
                    prior = self._gap_value(namespace, gap_id)
                    status = (
                        "open"
                        if not prior or prior["status"] in {"resolved", "dismissed"}
                        else prior["status"]
                    )
                    evidence_ids = sorted(
                        {
                            str(item["evidence_id"])
                            for item in condition["evidence"]
                            if item.get("evidence_id")
                        }
                    )
                    detail = {
                        "actual": condition["actual"],
                        "required": condition["required"],
                        "evidence_ids": evidence_ids,
                        "signals": coverage["signals"],
                    }
                    explanation = {
                        "reason": condition["gap_type"],
                        "thresholds": selected["thresholds"],
                        "evidence_count": len(evidence_ids),
                        "silently_inferred": False,
                    }
                    revision_id, idempotent = self._write_gap_revision(
                        gap_id,
                        namespace,
                        status,
                        selected["policy_id"],
                        coverage,
                        detail,
                        explanation,
                        principal_id=principal_id,
                        now=now,
                    )
                    value = self._gap_value(namespace, gap_id)
                    value.update(
                        {"gap_revision_id": revision_id, "idempotent": idempotent}
                    )
                    items.append(value)
                    mutated = mutated or not idempotent
                stale_rows = self.conn.execute(
                    "SELECT g.gap_id,g.gap_type FROM research_gaps g JOIN research_gap_current c ON c.gap_id=g.gap_id JOIN research_gap_revisions r ON r.gap_revision_id=c.gap_revision_id WHERE g.namespace=? AND g.object_kind=? AND g.object_id=? AND g.dimension_key=? AND r.status IN ('open','in-progress') ORDER BY g.gap_id",
                    [
                        namespace,
                        coverage["object_kind"],
                        coverage["object_id"],
                        coverage["dimension_key"],
                    ],
                ).fetchall()
                for gap_id, gap_type in stale_rows:
                    if gap_type in detected_types:
                        continue
                    _, idempotent = self._write_gap_revision(
                        gap_id,
                        namespace,
                        "resolved",
                        selected["policy_id"],
                        coverage,
                        {
                            "resolution": "coverage-improved",
                            "signals": coverage["signals"],
                        },
                        {
                            "reason": "condition-no-longer-detected",
                            "silently_inferred": False,
                        },
                        principal_id=principal_id,
                        now=now,
                    )
                    mutated = mutated or not idempotent
            report_hash = _digest([item["gap_revision_id"] for item in items])
            if mutated:
                self._audit(
                    namespace,
                    "discover",
                    report_hash,
                    principal_id,
                    {"scanned": len(rows)},
                    now,
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "contract": REPORT_CONTRACT,
            "namespace": namespace,
            "status": "completed",
            "policy_id": selected["policy_id"],
            "items": items,
            "scanned": len(rows),
            "report_hash": report_hash,
        }

    def list(
        self,
        namespace: str,
        *,
        scopes: set[str],
        status: str | None = None,
        gap_type: str | None = None,
        object_kind: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        start = _offset(cursor)
        size = min(max(limit, 1), 500)
        rows = self.conn.execute(
            "SELECT g.gap_id FROM research_gaps g JOIN research_gap_current c ON c.gap_id=g.gap_id JOIN research_gap_revisions r ON r.gap_revision_id=c.gap_revision_id WHERE g.namespace=? AND (? IS NULL OR r.status=?) AND (? IS NULL OR g.gap_type=?) AND (? IS NULL OR g.object_kind=?) ORDER BY g.gap_id LIMIT ? OFFSET ?",
            [
                namespace,
                status,
                status,
                gap_type,
                gap_type,
                object_kind,
                object_kind,
                size + 1,
                start,
            ],
        ).fetchall()
        has_more = len(rows) > size
        items = [self._gap_value(namespace, row[0]) for row in rows[:size]]
        return {
            "contract": REPORT_CONTRACT,
            "namespace": namespace,
            "status": "completed",
            "items": items,
            "next_cursor": _cursor(start + size) if has_more else None,
            "report_hash": _digest([item["gap_revision_id"] for item in items]),
        }

    def set_status(
        self,
        namespace: str,
        gap_id: str,
        status: str,
        *,
        reason: str,
        evidence: Sequence[Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, REVIEW_SCOPE)
        if status not in GAP_STATUSES or not reason.strip() or not evidence:
            raise ResearchGapError(
                "invalid_status", "status, reason, and evidence are required"
            )
        current = self._gap_value(namespace, gap_id)
        if not current:
            raise ResearchGapError("gap_not_found", "research gap does not exist")
        now = self.now()
        coverage = {
            **self._coverage(namespace, current["observation_id"]),
            "observed_at_ms": now,
        }
        detail = {
            **current["detail"],
            "review": {"reason": reason, "evidence": [dict(item) for item in evidence]},
        }
        self.conn.execute("BEGIN")
        try:
            _, idempotent = self._write_gap_revision(
                gap_id,
                namespace,
                status,
                current["policy_id"],
                coverage,
                detail,
                {**current["explanation"], "reviewed": True},
                principal_id=principal_id,
                now=now,
            )
            if not idempotent:
                self._audit(
                    namespace,
                    "set-status",
                    gap_id,
                    principal_id,
                    {"status": status},
                    now,
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**self._gap_value(namespace, gap_id), "idempotent": idempotent}

    @staticmethod
    def _task_action(gap_type: str) -> str:
        return {
            "missing-primary": "acquire-primary-source",
            "missing-original-source": "trace-citation-to-original",
            "circular-citation": "break-citation-cycle",
            "unresolved-contradiction": "adjudicate-contradiction",
            "insufficient-independent-support": "find-independent-corroboration",
            "missing-current-support": "refresh-evidence",
            "methodologically-inadequate": "find-methodologically-adequate-source",
            "inaccessible-evidence": "find-accessible-equivalent",
            "retracted-support": "replace-retracted-support",
            "unknown-coverage": "establish-coverage-baseline",
        }.get(gap_type, "research-gap")

    def prioritize(
        self,
        namespace: str,
        *,
        budget: float,
        principal_id: str,
        scopes: set[str],
        max_tasks: int = 25,
        blocked_source_classes: Sequence[str] = (),
        policy_version: str | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE if persist else READ_SCOPE)
        if budget < 0:
            raise ResearchGapError("invalid_budget", "budget must be non-negative")
        policy = self.select_policy(
            namespace, scopes={READ_SCOPE}, semantic_version=policy_version
        )
        gaps = self.list(namespace, scopes={READ_SCOPE}, status="open", limit=500)[
            "items"
        ]
        blocked = set(blocked_source_classes)
        candidates = []
        for gap in gaps:
            signals = gap["detail"].get("signals", {})
            source_class = str(
                signals.get("recommended_source_class")
                or gap["dimension"].get("source_class")
                or "unspecified"
            )
            if source_class in blocked:
                continue
            cost = max(0.0, float(signals.get("estimated_cost", 1)))
            components = {
                "decision_relevance": float(signals.get("decision_relevance", 0.5)),
                "uncertainty_reduction": float(
                    signals.get("uncertainty_reduction", 0.5)
                ),
                "feasibility": float(signals.get("feasibility", 0.5)),
                "freshness": float(signals.get("freshness", 0.5)),
                "policy_priority": float(signals.get("policy_priority", 0.5)),
            }
            score = sum(
                policy["weights"][key] * value for key, value in components.items()
            )
            score -= policy["weights"].get("cost", 0) * min(cost / max(budget, 1), 1)
            candidates.append(
                (round(score, 8), gap["gap_id"], gap, cost, source_class, components)
            )
        candidates.sort(key=lambda item: (-item[0], item[1]))
        selected = []
        spent = 0.0
        for score, gap_id, gap, cost, source_class, components in candidates:
            if len(selected) >= min(max(max_tasks, 0), 500) or spent + cost > budget:
                continue
            suggestion = {
                "action": self._task_action(gap["gap_type"]),
                "object_kind": gap["object_kind"],
                "object_id": gap["object_id"],
                "source_class": source_class,
                "query": gap["dimension"].get("query") or gap["object_id"],
                "constraints": gap["dimension"],
                "evidence_required": gap["detail"].get("required"),
            }
            selected.append(
                {
                    "gap": gap,
                    "score": score,
                    "cost": cost,
                    "source_class": source_class,
                    "components": components,
                    "suggestion": suggestion,
                }
            )
            spent += cost
        plan_hash = _digest(
            [
                policy["policy_id"],
                budget,
                blocked_source_classes,
                [(item["gap"]["gap_id"], item["score"]) for item in selected],
            ]
        )
        tasks = []
        mutated = False
        now = self.now()
        if persist:
            self.conn.execute("BEGIN")
        try:
            for rank, item in enumerate(selected, 1):
                gap = item["gap"]
                task_id = (
                    "research-gap-task:"
                    + _digest(
                        [namespace, gap["gap_id"], policy["policy_id"], plan_hash]
                    )[:24]
                )
                existing = self.conn.execute(
                    "SELECT created_at_ms FROM research_gap_tasks WHERE task_id=?",
                    [task_id],
                ).fetchone()
                if persist and not existing:
                    self.conn.execute(
                        "INSERT INTO research_gap_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [
                            task_id,
                            namespace,
                            gap["gap_id"],
                            policy["policy_id"],
                            rank,
                            item["score"],
                            item["cost"],
                            _canonical(item["suggestion"]),
                            "proposed",
                            plan_hash,
                            principal_id,
                            now,
                        ],
                    )
                    mutated = True
                tasks.append(
                    {
                        "contract": TASK_CONTRACT,
                        "task_id": task_id,
                        "namespace": namespace,
                        "gap_id": gap["gap_id"],
                        "policy_id": policy["policy_id"],
                        "rank": rank,
                        "score": item["score"],
                        "score_components": item["components"],
                        "estimated_cost": item["cost"],
                        "suggestion": item["suggestion"],
                        "status": "proposed",
                        "plan_hash": plan_hash,
                        "created_at_ms": int(existing[0]) if existing else now,
                        "idempotent": bool(existing),
                    }
                )
            if persist and mutated:
                self._audit(
                    namespace,
                    "prioritize",
                    plan_hash,
                    principal_id,
                    {"tasks": len(tasks), "budget": budget},
                    now,
                )
            if persist:
                self.conn.execute("COMMIT")
        except Exception:
            if persist:
                self.conn.execute("ROLLBACK")
            raise
        return {
            "namespace": namespace,
            "policy_id": policy["policy_id"],
            "budget": budget,
            "spent": round(spent, 8),
            "tasks": tasks,
            "plan_hash": plan_hash,
        }

    def tasks(
        self,
        namespace: str,
        *,
        scopes: set[str],
        status: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        start = _offset(cursor)
        size = min(max(limit, 1), 500)
        rows = self.conn.execute(
            "SELECT task_id,gap_id,policy_id,rank,score,estimated_cost,suggestion_json,status,plan_hash,principal_id,created_at_ms FROM research_gap_tasks WHERE namespace=? AND (? IS NULL OR status=?) ORDER BY rank,task_id LIMIT ? OFFSET ?",
            [namespace, status, status, size + 1, start],
        ).fetchall()
        has_more = len(rows) > size
        items = [
            {
                "contract": TASK_CONTRACT,
                "task_id": row[0],
                "namespace": namespace,
                "gap_id": row[1],
                "policy_id": row[2],
                "rank": int(row[3]),
                "score": float(row[4]),
                "estimated_cost": float(row[5]),
                "suggestion": _load(row[6], {}),
                "status": row[7],
                "plan_hash": row[8],
                "principal_id": row[9],
                "created_at_ms": int(row[10]),
            }
            for row in rows[:size]
        ]
        return {
            "namespace": namespace,
            "items": items,
            "next_cursor": _cursor(start + size) if has_more else None,
            "result_hash": _digest([item["task_id"] for item in items]),
        }

    def compare_coverage(
        self,
        namespace: str,
        before_observed_ms: int,
        after_observed_ms: int,
        *,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        if after_observed_ms < before_observed_ms:
            raise ResearchGapError("invalid_window", "after must not precede before")

        def snapshot(at_ms: int) -> dict[str, Any]:
            rows = self.conn.execute(
                "SELECT status FROM (SELECT r.status,row_number() OVER (PARTITION BY r.gap_id ORDER BY r.revision DESC) AS position FROM research_gap_revisions r WHERE r.namespace=? AND r.observed_at_ms<=?) ranked WHERE position=1",
                [namespace, at_ms],
            ).fetchall()
            counts = {status: 0 for status in GAP_STATUSES}
            for row in rows:
                counts[row[0]] += 1
            total = len(rows)
            closed = counts["resolved"] + counts["dismissed"]
            return {
                "at_ms": at_ms,
                "counts": counts,
                "total": total,
                "coverage_score": round(closed / total, 8) if total else None,
            }

        before = snapshot(before_observed_ms)
        after = snapshot(after_observed_ms)
        return {
            "namespace": namespace,
            "before": before,
            "after": after,
            "resolved_delta": after["counts"]["resolved"]
            - before["counts"]["resolved"],
            "comparison_hash": _digest([before, after]),
        }

    def replay(
        self, namespace: str, gap_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        gap = self._gap_value(namespace, gap_id)
        if not gap:
            raise ResearchGapError("gap_not_found", "research gap does not exist")
        semantic = {
            "status": gap["status"],
            "policy_id": gap["policy_id"],
            "observation_id": gap["observation_id"],
            "detail": gap["detail"],
            "explanation": gap["explanation"],
        }
        replayed = _digest(semantic)
        return {
            "gap_id": gap_id,
            "stored_hash": gap["content_hash"],
            "replayed_hash": replayed,
            "deterministic": replayed == gap["content_hash"],
        }
