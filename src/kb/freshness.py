"""Versioned evidence freshness policies, supersession, assessment, and propagation."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

POLICY_CONTRACT = "noesis-evidence-freshness-policy-v1"
ASSESSMENT_CONTRACT = "noesis-evidence-freshness-assessment-v1"
RELATION_CONTRACT = "noesis-evidence-applicability-relation-v1"
IMPACT_CONTRACT = "noesis-evidence-freshness-impact-v1"
READ_SCOPE = "knowledge:freshness:read"
WRITE_SCOPE = "knowledge:freshness:write"
REVIEW_SCOPE = "knowledge:freshness:review"
RELATIONS = {"supersedes", "narrows", "invalidates", "no-longer-applies"}
STATES = {
    "fresh",
    "expiring-soon",
    "stale",
    "expired",
    "unknown",
    "timeless",
    "invalid",
}
CONSUMERS = {"claim", "answer", "brief", "watch", "search", "assessment"}

_DDL = """
CREATE TABLE IF NOT EXISTS evidence_freshness_policies (
  policy_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, domain TEXT NOT NULL,
  source_type TEXT NOT NULL, object_type TEXT NOT NULL, semantic_version TEXT NOT NULL,
  rules_json TEXT NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL,
  supersedes_policy_id TEXT, generation BIGINT NOT NULL, valid_from_ms BIGINT,
  valid_to_ms BIGINT, observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL,
  policy_context_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,domain,source_type,object_type,semantic_version)
);
CREATE TABLE IF NOT EXISTS evidence_freshness_inputs (
  evidence_id TEXT NOT NULL, namespace TEXT NOT NULL, domain TEXT NOT NULL,
  source_type TEXT NOT NULL, object_type TEXT NOT NULL, published_at_ms BIGINT,
  observed_at_ms BIGINT, retrieved_at_ms BIGINT NOT NULL, valid_from_ms BIGINT,
  valid_to_ms BIGINT, event_closed_at_ms BIGINT, methodology_revision TEXT,
  source_health_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
  generation BIGINT NOT NULL, producer_json TEXT NOT NULL, policy_json TEXT NOT NULL,
  principal_id TEXT NOT NULL, input_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace,evidence_id)
);
CREATE TABLE IF NOT EXISTS evidence_applicability_relations (
  relation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, earlier_evidence_id TEXT NOT NULL,
  later_evidence_id TEXT NOT NULL, relation TEXT NOT NULL, applicability_json TEXT NOT NULL,
  confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
  generation BIGINT NOT NULL, valid_from_ms BIGINT, valid_to_ms BIGINT,
  observed_at_ms BIGINT NOT NULL, principal_id TEXT NOT NULL, input_hash TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,earlier_evidence_id,later_evidence_id,relation)
);
CREATE TABLE IF NOT EXISTS evidence_freshness_overrides (
  override_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, evidence_id TEXT NOT NULL,
  state TEXT NOT NULL, valid_until_ms BIGINT, reason TEXT NOT NULL,
  evidence_json TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_freshness_assessments (
  assessment_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, evidence_id TEXT NOT NULL,
  policy_id TEXT NOT NULL, assessed_at_ms BIGINT NOT NULL, context_json TEXT NOT NULL,
  result_json TEXT NOT NULL, calculation_hash TEXT NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_freshness_dependencies (
  namespace TEXT NOT NULL, evidence_id TEXT NOT NULL, consumer_kind TEXT NOT NULL,
  consumer_id TEXT NOT NULL, detail_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace,evidence_id,consumer_kind,consumer_id)
);
CREATE TABLE IF NOT EXISTS evidence_freshness_impacts (
  impact_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, consumer_kind TEXT NOT NULL,
  consumer_id TEXT NOT NULL, state TEXT NOT NULL, evidence_states_json TEXT NOT NULL,
  ranking_factor DOUBLE NOT NULL, reason TEXT NOT NULL, assessment_hash TEXT NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,consumer_kind,consumer_id,assessment_hash)
);
CREATE TABLE IF NOT EXISTS evidence_freshness_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
"""


class FreshnessError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return (
        default
        if value is None
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise FreshnessError("unauthorized", f"missing required scope {required}")


class EvidenceFreshnessStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(self, namespace, operation, object_id, principal_id, detail, now):
        audit_id = (
            "freshness-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO evidence_freshness_audit VALUES (?,?,?,?,?,?,?)",
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
        domain: str,
        source_type: str,
        object_type: str,
        semantic_version: str,
        rules: Mapping[str, Any],
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
        if not all(
            str(value).strip()
            for value in (domain, source_type, object_type, semantic_version)
        ):
            raise FreshnessError(
                "invalid_policy", "domain, source, object, and version are required"
            )
        values = dict(rules)
        missing = str(values.get("missing_date", "unknown"))
        if missing not in {"unknown", "timeless", "stale"}:
            raise FreshnessError(
                "invalid_policy", "missing_date must be unknown, timeless, or stale"
            )
        for key in (
            "max_age_ms",
            "warning_before_ms",
            "decay_half_life_ms",
            "cadence_ms",
            "event_close_grace_ms",
        ):
            if values.get(key) is not None and int(values[key]) < 0:
                raise FreshnessError("invalid_policy", f"{key} must be non-negative")
        values.setdefault("missing_date", missing)
        values.setdefault("warning_before_ms", 0)
        values.setdefault("source_health_required", False)
        values.setdefault("methodology_change", "reassess")
        now = self.now()
        stable = {
            "namespace": namespace,
            "domain": domain,
            "source_type": source_type,
            "object_type": object_type,
            "semantic_version": semantic_version,
            "rules": values,
            "supersedes_policy_id": supersedes_policy_id,
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
            "producer": dict(
                producer or {"name": "noesis-evidence-freshness", "version": "1.0.0"}
            ),
            "policy_context": dict(policy_context or {}),
            "provenance": dict(provenance or {}),
        }
        # A server-assigned observation timestamp is receipt metadata, not semantic
        # policy content. Excluding it makes retries idempotent without weakening
        # immutable semantic versions.
        content_hash = _digest(
            {key: value for key, value in stable.items() if key != "observed_at_ms"}
        )
        policy_id = (
            "freshness-policy:"
            + _digest([namespace, domain, source_type, object_type, semantic_version])[
                :24
            ]
        )
        existing = self.conn.execute(
            "SELECT content_hash FROM evidence_freshness_policies WHERE policy_id=?",
            [policy_id],
        ).fetchone()
        if existing:
            if existing[0] != content_hash:
                raise FreshnessError(
                    "immutable_version",
                    "freshness policy version has different content",
                )
            return {
                **self.policy(policy_id, namespace=namespace, scopes={READ_SCOPE}),
                "idempotent": True,
            }
        if supersedes_policy_id:
            superseded = self.policy(
                supersedes_policy_id, namespace=namespace, scopes={READ_SCOPE}
            )
            if not superseded:
                raise FreshnessError(
                    "policy_not_found", "superseded policy does not exist"
                )
            if (
                superseded["domain"],
                superseded["source_type"],
                superseded["object_type"],
            ) != (domain, source_type, object_type):
                raise FreshnessError(
                    "invalid_policy", "policy upgrades must retain their selectors"
                )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO evidence_freshness_policies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    policy_id,
                    namespace,
                    domain,
                    source_type,
                    object_type,
                    semantic_version,
                    _canonical(values),
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
                    "UPDATE evidence_freshness_policies SET status='superseded' WHERE policy_id=?",
                    [supersedes_policy_id],
                )
            self._audit(
                namespace,
                "register-policy",
                policy_id,
                principal_id,
                {"version": semantic_version},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.policy(policy_id, namespace=namespace, scopes={READ_SCOPE})

    def policy(
        self,
        policy_id: str,
        *,
        scopes: set[str],
        namespace: str,
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT namespace,domain,source_type,object_type,semantic_version,rules_json,content_hash,status,supersedes_policy_id,generation,valid_from_ms,valid_to_ms,observed_at_ms,producer_json,policy_context_json,provenance_json,principal_id,created_at_ms FROM evidence_freshness_policies WHERE policy_id=? AND namespace=?",
            [policy_id, namespace],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": POLICY_CONTRACT,
            "policy_id": policy_id,
            "namespace": row[0],
            "domain": row[1],
            "source_type": row[2],
            "object_type": row[3],
            "semantic_version": row[4],
            "rules": _load(row[5], {}),
            "content_hash": row[6],
            "status": row[7],
            "supersedes_policy_id": row[8],
            "generation": int(row[9]),
            "valid_from_ms": row[10],
            "valid_to_ms": row[11],
            "observed_at_ms": int(row[12]),
            "producer": _load(row[13], {}),
            "policy": _load(row[14], {}),
            "provenance": _load(row[15], {}),
            "principal_id": row[16],
            "created_at_ms": int(row[17]),
        }

    def select_policy(
        self,
        namespace: str,
        domain: str,
        source_type: str,
        object_type: str,
        *,
        scopes: set[str],
        semantic_version: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT policy_id FROM evidence_freshness_policies WHERE namespace=? AND ((? IS NULL AND status='active') OR (? IS NOT NULL AND semantic_version=?)) AND domain IN (?, '*') AND source_type IN (?, '*') AND object_type IN (?, '*') ORDER BY (domain=?) DESC,(source_type=?) DESC,(object_type=?) DESC,observed_at_ms DESC,policy_id LIMIT 1",
            [
                namespace,
                semantic_version,
                semantic_version,
                semantic_version,
                domain,
                source_type,
                object_type,
                domain,
                source_type,
                object_type,
            ],
        ).fetchone()
        if not row:
            raise FreshnessError(
                "policy_not_found", "no applicable freshness policy exists"
            )
        return self.policy(row[0], namespace=namespace, scopes=scopes)

    def annotate(
        self,
        namespace: str,
        evidence_id: str,
        domain: str,
        source_type: str,
        object_type: str,
        *,
        retrieved_at_ms: int,
        principal_id: str,
        scopes: set[str],
        published_at_ms: int | None = None,
        observed_at_ms: int | None = None,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        event_closed_at_ms: int | None = None,
        methodology_revision: str | None = None,
        source_health: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
        generation: int = 0,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if not all(
            str(value).strip()
            for value in (namespace, evidence_id, domain, source_type, object_type)
        ):
            raise FreshnessError(
                "invalid_evidence",
                "namespace, evidence, domain, source, and object are required",
            )
        if (
            valid_from_ms is not None
            and valid_to_ms is not None
            and valid_to_ms <= valid_from_ms
        ):
            raise FreshnessError(
                "invalid_evidence", "valid_to_ms must follow valid_from_ms"
            )
        stable = {
            "evidence_id": evidence_id,
            "namespace": namespace,
            "domain": domain,
            "source_type": source_type,
            "object_type": object_type,
            "published_at_ms": published_at_ms,
            "observed_at_ms": observed_at_ms,
            "retrieved_at_ms": int(retrieved_at_ms),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "event_closed_at_ms": event_closed_at_ms,
            "methodology_revision": methodology_revision,
            "source_health": dict(source_health or {"status": "unknown"}),
            "provenance": dict(provenance or {}),
            "generation": int(generation),
            "producer": dict(
                producer or {"name": "noesis-evidence-freshness", "version": "1.0.0"}
            ),
            "policy": dict(policy or {}),
        }
        input_hash = _digest(stable)
        now = self.now()
        existing = self.conn.execute(
            "SELECT input_hash FROM evidence_freshness_inputs WHERE namespace=? AND evidence_id=?",
            [namespace, evidence_id],
        ).fetchone()
        if existing:
            if existing[0] != input_hash:
                raise FreshnessError(
                    "evidence_conflict",
                    "freshness annotation differs for evidence identity",
                )
            return {**stable, "input_hash": input_hash, "idempotent": True}
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO evidence_freshness_inputs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    evidence_id,
                    namespace,
                    domain,
                    source_type,
                    object_type,
                    published_at_ms,
                    observed_at_ms,
                    retrieved_at_ms,
                    valid_from_ms,
                    valid_to_ms,
                    event_closed_at_ms,
                    methodology_revision,
                    _canonical(stable["source_health"]),
                    _canonical(stable["provenance"]),
                    generation,
                    _canonical(stable["producer"]),
                    _canonical(stable["policy"]),
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            self._audit(namespace, "annotate", evidence_id, principal_id, {}, now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**stable, "input_hash": input_hash, "idempotent": False}

    def relate(
        self,
        namespace: str,
        earlier_evidence_id: str,
        later_evidence_id: str,
        relation: str,
        *,
        applicability: Mapping[str, Any],
        confidence: float,
        evidence: Sequence[Mapping[str, Any]],
        provenance: Mapping[str, Any],
        principal_id: str,
        scopes: set[str],
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        confidence_value = float(confidence)
        evidence_values = [dict(item) for item in evidence]
        if (
            earlier_evidence_id == later_evidence_id
            or relation not in RELATIONS
            or not 0 <= confidence_value <= 1
            or not evidence_values
        ):
            raise FreshnessError(
                "invalid_relation",
                "distinct evidence, relation, confidence, and evidence are required",
            )
        fraction = applicability.get("fraction", 1)
        if not 0 <= float(fraction) <= 1:
            raise FreshnessError(
                "invalid_relation",
                "applicability fraction must be between zero and one",
            )
        if (
            valid_from_ms is not None
            and valid_to_ms is not None
            and valid_to_ms <= valid_from_ms
        ):
            raise FreshnessError(
                "invalid_relation", "valid_to_ms must follow valid_from_ms"
            )
        for evidence_id in (earlier_evidence_id, later_evidence_id):
            if not self.conn.execute(
                "SELECT 1 FROM evidence_freshness_inputs WHERE namespace=? AND evidence_id=?",
                [namespace, evidence_id],
            ).fetchone():
                raise FreshnessError(
                    "evidence_not_found", f"evidence {evidence_id!r} is not annotated"
                )
        now = self.now()
        stable = {
            "namespace": namespace,
            "earlier_evidence_id": earlier_evidence_id,
            "later_evidence_id": later_evidence_id,
            "relation": relation,
            "applicability": dict(applicability),
            "confidence": confidence_value,
            "evidence": evidence_values,
            "provenance": dict(provenance),
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
        }
        input_hash = _digest({k: v for k, v in stable.items() if k != "observed_at_ms"})
        relation_id = (
            "evidence-relation:"
            + _digest(
                [
                    namespace,
                    earlier_evidence_id,
                    later_evidence_id,
                    relation,
                    applicability,
                ]
            )[:24]
        )
        existing = self.conn.execute(
            "SELECT input_hash FROM evidence_applicability_relations WHERE relation_id=?",
            [relation_id],
        ).fetchone()
        if existing:
            if existing[0] != input_hash:
                raise FreshnessError(
                    "relation_conflict", "applicability relation differs"
                )
            return {
                **self.relation(namespace, relation_id, scopes={READ_SCOPE}),
                "idempotent": True,
            }
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO evidence_applicability_relations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    relation_id,
                    namespace,
                    earlier_evidence_id,
                    later_evidence_id,
                    relation,
                    _canonical(stable["applicability"]),
                    confidence_value,
                    _canonical(evidence_values),
                    _canonical(stable["provenance"]),
                    generation,
                    valid_from_ms,
                    valid_to_ms,
                    stable["observed_at_ms"],
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            self._audit(
                namespace,
                "relate",
                relation_id,
                principal_id,
                {"relation": relation},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.relation(namespace, relation_id, scopes={READ_SCOPE})

    def relation(
        self, namespace: str, relation_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT namespace,earlier_evidence_id,later_evidence_id,relation,applicability_json,confidence,evidence_json,provenance_json,generation,valid_from_ms,valid_to_ms,observed_at_ms,principal_id,input_hash,created_at_ms FROM evidence_applicability_relations WHERE relation_id=? AND namespace=?",
            [relation_id, namespace],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": RELATION_CONTRACT,
            "relation_id": relation_id,
            "namespace": row[0],
            "earlier_evidence_id": row[1],
            "later_evidence_id": row[2],
            "relation": row[3],
            "applicability": _load(row[4], {}),
            "confidence": float(row[5]),
            "evidence": _load(row[6], []),
            "provenance": _load(row[7], {}),
            "generation": int(row[8]),
            "valid_from_ms": row[9],
            "valid_to_ms": row[10],
            "observed_at_ms": int(row[11]),
            "principal_id": row[12],
            "input_hash": row[13],
            "created_at_ms": int(row[14]),
        }

    def override(
        self,
        namespace: str,
        evidence_id: str,
        state: str,
        *,
        valid_until_ms: int | None,
        reason: str,
        evidence: Sequence[Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, REVIEW_SCOPE)
        if state not in STATES or not reason.strip() or not evidence:
            raise FreshnessError(
                "invalid_override",
                "state, reason, and supporting evidence are required",
            )
        self._input(namespace, evidence_id)
        stable = [namespace, evidence_id, state, valid_until_ms, reason, evidence]
        override_id = "freshness-override:" + _digest(stable)[:24]
        existing = self.conn.execute(
            "SELECT evidence_json,principal_id,created_at_ms FROM evidence_freshness_overrides WHERE override_id=?",
            [override_id],
        ).fetchone()
        now = self.now()
        if not existing:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    "INSERT INTO evidence_freshness_overrides VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        override_id,
                        namespace,
                        evidence_id,
                        state,
                        valid_until_ms,
                        reason,
                        _canonical([dict(item) for item in evidence]),
                        principal_id,
                        now,
                    ],
                )
                self._audit(
                    namespace,
                    "override",
                    override_id,
                    principal_id,
                    {"state": state},
                    now,
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {
            "override_id": override_id,
            "namespace": namespace,
            "evidence_id": evidence_id,
            "state": state,
            "valid_until_ms": valid_until_ms,
            "reason": reason,
            "evidence": _load(existing[0], [])
            if existing
            else [dict(item) for item in evidence],
            "principal_id": existing[1] if existing else principal_id,
            "created_at_ms": int(existing[2]) if existing else now,
            "idempotent": bool(existing),
        }

    def _input(self, namespace, evidence_id):
        row = self.conn.execute(
            "SELECT domain,source_type,object_type,published_at_ms,observed_at_ms,retrieved_at_ms,valid_from_ms,valid_to_ms,event_closed_at_ms,methodology_revision,source_health_json,provenance_json,generation,producer_json,policy_json,input_hash FROM evidence_freshness_inputs WHERE namespace=? AND evidence_id=?",
            [namespace, evidence_id],
        ).fetchone()
        if not row:
            raise FreshnessError("evidence_not_found", "evidence is not annotated")
        keys = (
            "domain",
            "source_type",
            "object_type",
            "published_at_ms",
            "observed_at_ms",
            "retrieved_at_ms",
            "valid_from_ms",
            "valid_to_ms",
            "event_closed_at_ms",
            "methodology_revision",
            "source_health",
            "provenance",
            "generation",
            "producer",
            "policy",
            "input_hash",
        )
        values = list(row)
        values[10] = _load(values[10], {})
        values[11] = _load(values[11], {})
        values[13] = _load(values[13], {})
        values[14] = _load(values[14], {})
        return dict(zip(keys, values))

    def assess(
        self,
        namespace: str,
        evidence_id: str,
        *,
        at_ms: int,
        scopes: set[str],
        principal_id: str = "simulation",
        policy_version: str | None = None,
        context: Mapping[str, Any] | None = None,
        persist: bool = True,
        policy_override: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE if persist else READ_SCOPE)
        item = self._input(namespace, evidence_id)
        ctx = dict(context or {})
        selected = self.select_policy(
            namespace,
            item["domain"],
            item["source_type"],
            item["object_type"],
            scopes={READ_SCOPE},
            semantic_version=policy_version,
        )
        rules = {**selected["rules"], **dict(policy_override or {})}
        timestamp = (
            item["published_at_ms"]
            if item["published_at_ms"] is not None
            else item["observed_at_ms"]
        )
        reasons = []
        valid = not (item["valid_to_ms"] is not None and at_ms >= item["valid_to_ms"])
        if timestamp is not None and timestamp > at_ms:
            state = "invalid"
            valid = False
            reasons.append("future-date")
        elif item["valid_from_ms"] is not None and at_ms < item["valid_from_ms"]:
            state = "invalid"
            valid = False
            reasons.append("validity-not-started")
        elif timestamp is None:
            state = rules.get("missing_date", "unknown")
            reasons.append("missing-date")
        elif not valid:
            state = "expired"
            reasons.append("validity-ended")
        elif item["event_closed_at_ms"] is not None and at_ms >= int(
            item["event_closed_at_ms"]
        ) + int(rules.get("event_close_grace_ms", 0)):
            state = "expired"
            valid = False
            reasons.append("event-closed")
        else:
            age = max(0, at_ms - int(timestamp))
            max_age = rules.get("max_age_ms")
            warning = int(rules.get("warning_before_ms", 0))
            if max_age is None:
                state = "timeless"
                reasons.append("timeless-policy")
            elif age > int(max_age):
                state = "stale"
                reasons.append("age-exceeded")
            elif age >= max(0, int(max_age) - warning):
                state = "expiring-soon"
                reasons.append("warning-window")
            else:
                state = "fresh"
                reasons.append("within-age-limit")
        cadence = rules.get("cadence_ms")
        if (
            cadence is not None
            and at_ms - int(item["retrieved_at_ms"]) > int(cadence)
            and state not in {"expired", "invalid"}
        ):
            state = "stale"
            reasons.append("source-cadence-overdue")
        relations = []
        for row in self.conn.execute(
            "SELECT relation_id FROM evidence_applicability_relations WHERE namespace=? AND earlier_evidence_id=? AND (valid_from_ms IS NULL OR valid_from_ms<=?) AND (valid_to_ms IS NULL OR valid_to_ms>?) ORDER BY relation_id",
            [namespace, evidence_id, at_ms, at_ms],
        ).fetchall():
            relation = self.relation(namespace, row[0], scopes={READ_SCOPE})
            applicability = relation["applicability"]
            jurisdiction = applicability.get("jurisdiction")
            if jurisdiction and ctx.get("jurisdiction") not in {jurisdiction, "*"}:
                continue
            relations.append(relation)
        terminal = [
            relation
            for relation in relations
            if relation["relation"] in {"invalidates", "no-longer-applies"}
            or relation["relation"] == "supersedes"
            and float(relation["applicability"].get("fraction", 1)) >= 1
        ]
        if terminal:
            state = "expired"
            valid = False
            reasons.extend(sorted({relation["relation"] for relation in terminal}))
        elif relations:
            reasons.extend(
                sorted({"partial-" + relation["relation"] for relation in relations})
            )
        if len({relation["later_evidence_id"] for relation in relations}) > 1:
            reasons.append("conflicting-successors")
        health = item["source_health"].get("status", "unknown")
        if rules.get("source_health_required") and health in {
            "down",
            "unhealthy",
            "retracted",
        }:
            state = "stale" if valid else state
            reasons.append("source-unhealthy")
        expected_methodology = rules.get("methodology_revision")
        if (
            expected_methodology
            and item["methodology_revision"] != expected_methodology
        ):
            if rules.get("methodology_change") == "invalidate":
                state = "invalid"
                valid = False
            elif state not in {"expired", "invalid"}:
                state = "stale"
            reasons.append("methodology-changed")
        override = self.conn.execute(
            "SELECT override_id,state,reason,evidence_json,valid_until_ms FROM evidence_freshness_overrides WHERE namespace=? AND evidence_id=? AND (valid_until_ms IS NULL OR valid_until_ms>?) ORDER BY created_at_ms DESC,override_id DESC LIMIT 1",
            [namespace, evidence_id, at_ms],
        ).fetchone()
        override_value = None
        if override:
            override_value = {
                "override_id": override[0],
                "state": override[1],
                "reason": override[2],
                "evidence": _load(override[3], []),
                "valid_until_ms": override[4],
            }
            state = override[1]
            valid = state not in {"expired", "invalid"}
            reasons.append("reviewed-override")
        age_ms = None if timestamp is None else at_ms - int(timestamp)
        half = rules.get("decay_half_life_ms")
        decay = (
            1.0
            if age_ms is None or half in {None, 0}
            else 0.5 ** (max(0, age_ms) / int(half))
        )
        if state in {"expired", "invalid"}:
            decay = 0.0
        result = {
            "contract": ASSESSMENT_CONTRACT,
            "namespace": namespace,
            "evidence_id": evidence_id,
            "policy_id": selected["policy_id"],
            "policy_version": selected["semantic_version"],
            "assessed_at_ms": int(at_ms),
            "state": state,
            "valid": valid,
            "age_ms": age_ms,
            "decay_score": round(decay, 8),
            "reasons": reasons,
            "relations": relations,
            "override": override_value,
            "source_health": item["source_health"],
            "context": ctx,
            "input_hash": item["input_hash"],
        }
        calculation_hash = _digest(result)
        assessment_id = "freshness-assessment:" + calculation_hash[:24]
        result.update(
            {"assessment_id": assessment_id, "calculation_hash": calculation_hash}
        )
        if persist:
            existing = self.conn.execute(
                "SELECT created_at_ms FROM evidence_freshness_assessments WHERE assessment_id=?",
                [assessment_id],
            ).fetchone()
            now = int(existing[0]) if existing else self.now()
            if not existing:
                self.conn.execute("BEGIN")
                try:
                    self.conn.execute(
                        "INSERT INTO evidence_freshness_assessments VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [
                            assessment_id,
                            namespace,
                            evidence_id,
                            selected["policy_id"],
                            at_ms,
                            _canonical(ctx),
                            _canonical(result),
                            calculation_hash,
                            principal_id,
                            now,
                        ],
                    )
                    self._audit(
                        namespace,
                        "assess",
                        assessment_id,
                        principal_id,
                        {"state": state},
                        now,
                    )
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            result["idempotent"] = bool(existing)
            result["created_at_ms"] = now
        return result

    def assessment(
        self, namespace: str, assessment_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        """Return a stored assessment with its complete explanation trace."""
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT result_json,principal_id,created_at_ms FROM evidence_freshness_assessments WHERE namespace=? AND assessment_id=?",
            [namespace, assessment_id],
        ).fetchone()
        if not row:
            return None
        return {
            **_load(row[0], {}),
            "principal_id": row[1],
            "created_at_ms": int(row[2]),
        }

    def replay(self, namespace, assessment_id, *, scopes: set[str]):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT result_json,calculation_hash FROM evidence_freshness_assessments WHERE namespace=? AND assessment_id=?",
            [namespace, assessment_id],
        ).fetchone()
        if not row:
            raise FreshnessError(
                "assessment_not_found", "freshness assessment does not exist"
            )
        stored = _load(row[0], {})
        base = {
            k: v
            for k, v in stored.items()
            if k
            not in {"assessment_id", "calculation_hash", "idempotent", "created_at_ms"}
        }
        replayed = _digest(base)
        return {
            "assessment_id": assessment_id,
            "stored_hash": row[1],
            "replayed_hash": replayed,
            "deterministic": replayed == row[1],
        }

    def expiring(
        self,
        namespace: str,
        *,
        at_ms: int,
        horizon_ms: int,
        scopes: set[str],
        limit: int = 100,
        cancel_requested: bool = False,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        if cancel_requested:
            return {"items": [], "status": "cancelled", "scanned": 0}
        ids = [
            row[0]
            for row in self.conn.execute(
                "SELECT evidence_id FROM evidence_freshness_inputs WHERE namespace=? ORDER BY evidence_id LIMIT ?",
                [namespace, min(max(limit, 1), 1000)],
            ).fetchall()
        ]
        items = []
        for evidence_id in ids:
            current = self.assess(
                namespace, evidence_id, at_ms=at_ms, scopes={READ_SCOPE}, persist=False
            )
            future = self.assess(
                namespace,
                evidence_id,
                at_ms=at_ms + horizon_ms,
                scopes={READ_SCOPE},
                persist=False,
            )
            if current["state"] not in {"expired", "invalid"} and future["state"] in {
                "expiring-soon",
                "stale",
                "expired",
            }:
                items.append(
                    {
                        "evidence_id": evidence_id,
                        "current_state": current["state"],
                        "future_state": future["state"],
                        "policy_id": current["policy_id"],
                    }
                )
        return {
            "items": items,
            "status": "completed",
            "scanned": len(ids),
            "at_ms": at_ms,
            "horizon_ms": horizon_ms,
        }

    def simulate(
        self,
        namespace: str,
        evidence_ids: Sequence[str],
        *,
        at_ms: int,
        scopes: set[str],
        policy_version: str | None = None,
        policy_override: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        limit: int = 100,
        cancel_requested: bool = False,
    ) -> dict[str, Any]:
        """Assess a bounded set without writing assessments, impacts, or audit rows."""
        _require(scopes, READ_SCOPE)
        if cancel_requested:
            return {"status": "cancelled", "items": [], "scanned": 0}
        bounded = sorted(set(evidence_ids))[: min(max(limit, 1), 1000)]
        items = [
            self.assess(
                namespace,
                evidence_id,
                at_ms=at_ms,
                scopes={READ_SCOPE},
                policy_version=policy_version,
                policy_override=policy_override,
                context=context,
                persist=False,
            )
            for evidence_id in bounded
        ]
        return {
            "status": "completed",
            "namespace": namespace,
            "at_ms": int(at_ms),
            "items": items,
            "scanned": len(bounded),
            "simulation_hash": _digest(items),
        }

    def compare_policies(
        self,
        namespace: str,
        evidence_ids: Sequence[str],
        *,
        at_ms: int,
        old_version: str,
        new_version: str,
        scopes: set[str],
        context: Mapping[str, Any] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Compare two immutable versions without changing the active policy."""
        _require(scopes, READ_SCOPE)
        bounded = sorted(set(evidence_ids))[: min(max(limit, 1), 1000)]
        transitions = []
        for evidence_id in bounded:
            old = self.assess(
                namespace,
                evidence_id,
                at_ms=at_ms,
                scopes={READ_SCOPE},
                policy_version=old_version,
                context=context,
                persist=False,
            )
            new = self.assess(
                namespace,
                evidence_id,
                at_ms=at_ms,
                scopes={READ_SCOPE},
                policy_version=new_version,
                context=context,
                persist=False,
            )
            transitions.append(
                {
                    "evidence_id": evidence_id,
                    "old_state": old["state"],
                    "new_state": new["state"],
                    "old_policy_id": old["policy_id"],
                    "new_policy_id": new["policy_id"],
                    "changed": old["state"] != new["state"],
                }
            )
        return {
            "namespace": namespace,
            "at_ms": int(at_ms),
            "old_version": old_version,
            "new_version": new_version,
            "transitions": transitions,
            "changed": sum(item["changed"] for item in transitions),
            "comparison_hash": _digest(transitions),
        }

    def dependency(
        self,
        namespace,
        evidence_id,
        consumer_kind,
        consumer_id,
        detail,
        *,
        principal_id,
        scopes,
    ):
        _require(scopes, WRITE_SCOPE)
        if consumer_kind not in CONSUMERS:
            raise FreshnessError("invalid_consumer", "unsupported freshness consumer")
        self._input(namespace, evidence_id)
        detail_value = _canonical(dict(detail))
        existing = self.conn.execute(
            "SELECT detail_json,principal_id,created_at_ms FROM evidence_freshness_dependencies WHERE namespace=? AND evidence_id=? AND consumer_kind=? AND consumer_id=?",
            [namespace, evidence_id, consumer_kind, consumer_id],
        ).fetchone()
        if existing and existing[0] != detail_value:
            raise FreshnessError(
                "dependency_conflict", "freshness dependency metadata differs"
            )
        now = self.now()
        if not existing:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    "INSERT INTO evidence_freshness_dependencies VALUES (?,?,?,?,?,?,?)",
                    [
                        namespace,
                        evidence_id,
                        consumer_kind,
                        consumer_id,
                        detail_value,
                        principal_id,
                        now,
                    ],
                )
                object_id = f"{consumer_kind}:{consumer_id}:{evidence_id}"
                self._audit(
                    namespace,
                    "register-dependency",
                    object_id,
                    principal_id,
                    {},
                    now,
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {
            "namespace": namespace,
            "evidence_id": evidence_id,
            "consumer_kind": consumer_kind,
            "consumer_id": consumer_id,
            "detail": _load(existing[0], {}) if existing else dict(detail),
            "created_at_ms": int(existing[2]) if existing else now,
            "idempotent": bool(existing),
        }

    def propagate(
        self,
        namespace: str,
        *,
        at_ms: int,
        principal_id: str,
        scopes: set[str],
        limit: int = 100,
        cancel_requested: bool = False,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if cancel_requested:
            return {"status": "cancelled", "items": [], "scanned": 0}
        consumers = self.conn.execute(
            "SELECT DISTINCT consumer_kind,consumer_id FROM evidence_freshness_dependencies WHERE namespace=? ORDER BY consumer_kind,consumer_id LIMIT ?",
            [namespace, min(max(limit, 1), 1000)],
        ).fetchall()
        items = []
        for kind, consumer_id in consumers:
            ids = [
                row[0]
                for row in self.conn.execute(
                    "SELECT evidence_id FROM evidence_freshness_dependencies WHERE namespace=? AND consumer_kind=? AND consumer_id=? ORDER BY evidence_id",
                    [namespace, kind, consumer_id],
                ).fetchall()
            ]
            assessments = [
                self.assess(
                    namespace,
                    evidence_id,
                    at_ms=at_ms,
                    scopes={WRITE_SCOPE},
                    principal_id=principal_id,
                )
                for evidence_id in ids
            ]
            current = [
                item
                for item in assessments
                if item["state"] in {"fresh", "expiring-soon", "timeless"}
                and item["valid"]
            ]
            state = (
                "current"
                if len(current) == len(assessments)
                else "mixed-age"
                if current
                else "unsupported-currently"
            )
            ranking = (
                sum(item["decay_score"] for item in assessments) / len(assessments)
                if assessments
                else 0
            )
            states = [
                {
                    "evidence_id": item["evidence_id"],
                    "assessment_id": item["assessment_id"],
                    "state": item["state"],
                }
                for item in assessments
            ]
            assessment_hash = _digest(states)
            impact_id = (
                "freshness-impact:"
                + _digest([namespace, kind, consumer_id, assessment_hash])[:24]
            )
            existing = self.conn.execute(
                "SELECT created_at_ms FROM evidence_freshness_impacts WHERE impact_id=?",
                [impact_id],
            ).fetchone()
            prior = self.conn.execute(
                "SELECT state FROM evidence_freshness_impacts WHERE namespace=? AND consumer_kind=? AND consumer_id=? ORDER BY created_at_ms DESC,impact_id DESC LIMIT 1",
                [namespace, kind, consumer_id],
            ).fetchone()
            reason = (
                "freshness-recovered"
                if prior and prior[0] != "current" and state == "current"
                else "last-current-support-lost"
                if not current
                else "mixed-evidence-age"
                if state == "mixed-age"
                else "evidence-current"
            )
            now = int(existing[0]) if existing else self.now()
            if not existing:
                self.conn.execute("BEGIN")
                try:
                    self.conn.execute(
                        "INSERT INTO evidence_freshness_impacts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        [
                            impact_id,
                            namespace,
                            kind,
                            consumer_id,
                            state,
                            _canonical(states),
                            round(ranking, 8),
                            reason,
                            assessment_hash,
                            principal_id,
                            now,
                        ],
                    )
                    self._audit(
                        namespace,
                        "propagate",
                        impact_id,
                        principal_id,
                        {"consumer_kind": kind, "state": state},
                        now,
                    )
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            items.append(
                {
                    "contract": IMPACT_CONTRACT,
                    "impact_id": impact_id,
                    "namespace": namespace,
                    "consumer_kind": kind,
                    "consumer_id": consumer_id,
                    "state": state,
                    "evidence_states": states,
                    "ranking_factor": round(ranking, 8),
                    "reason": reason,
                    "assessment_hash": assessment_hash,
                    "created_at_ms": now,
                    "idempotent": bool(existing),
                }
            )
        return {
            "status": "completed",
            "items": items,
            "scanned": len(consumers),
            "at_ms": at_ms,
        }
