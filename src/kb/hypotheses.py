"""Versioned, evidence-linked workspaces for comparing competing hypotheses."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

WORKSPACE_CONTRACT = "noesis-hypothesis-workspace-v1"
COMPARISON_CONTRACT = "noesis-hypothesis-comparison-v1"
PLAN_CONTRACT = "noesis-hypothesis-research-plan-v1"
EXPORT_CONTRACT = "noesis-hypothesis-export-v1"
READ_SCOPE = "knowledge:hypothesis:read"
WRITE_SCOPE = "knowledge:hypothesis:write"
EXECUTE_SCOPE = "knowledge:hypothesis:execute"
WORKSPACE_STATES = ("draft", "active", "retired")
STANCES = ("support", "contradict", "ambiguous")

_DDL = """
CREATE TABLE IF NOT EXISTS hypothesis_workspaces (
  workspace_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, parent_workspace_id TEXT, idempotency_key TEXT,
  UNIQUE(namespace,idempotency_key)
);
CREATE TABLE IF NOT EXISTS hypothesis_workspace_revisions (
  revision_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, namespace TEXT NOT NULL,
  revision BIGINT NOT NULL, predecessor_revision_id TEXT, title TEXT NOT NULL,
  lifecycle TEXT NOT NULL, hypotheses_json TEXT NOT NULL, generation BIGINT NOT NULL,
  valid_from_ms BIGINT, valid_to_ms BIGINT, observed_at_ms BIGINT NOT NULL,
  producer_json TEXT NOT NULL, policy_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  input_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(workspace_id,revision)
);
CREATE TABLE IF NOT EXISTS hypothesis_workspace_current (
  workspace_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, revision BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS hypothesis_evidence_revisions (
  link_revision_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, namespace TEXT NOT NULL,
  link_id TEXT NOT NULL, revision BIGINT NOT NULL, hypothesis_id TEXT NOT NULL,
  evidence_id TEXT NOT NULL, source_revision_id TEXT, stance TEXT NOT NULL,
  relevance DOUBLE NOT NULL, independence_group TEXT NOT NULL,
  provenance_json TEXT NOT NULL, annotations_json TEXT NOT NULL,
  required_scope TEXT, lifecycle TEXT NOT NULL, principal_id TEXT NOT NULL,
  input_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(workspace_id,link_id,revision)
);
CREATE TABLE IF NOT EXISTS hypothesis_evidence_current (
  workspace_id TEXT NOT NULL, link_id TEXT NOT NULL, link_revision_id TEXT NOT NULL,
  revision BIGINT NOT NULL, PRIMARY KEY(workspace_id,link_id)
);
CREATE TABLE IF NOT EXISTS hypothesis_research_plans (
  plan_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, namespace TEXT NOT NULL,
  workspace_revision_id TEXT NOT NULL, steps_json TEXT NOT NULL, cursor BIGINT NOT NULL,
  results_json TEXT NOT NULL, gaps_json TEXT NOT NULL, status TEXT NOT NULL,
  budget_used DOUBLE NOT NULL, plan_hash TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS hypothesis_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, workspace_id TEXT NOT NULL,
  operation TEXT NOT NULL, object_id TEXT NOT NULL, principal_id TEXT NOT NULL,
  detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hypothesis_workspace_namespace
  ON hypothesis_workspaces(namespace,workspace_id);
CREATE INDEX IF NOT EXISTS idx_hypothesis_evidence_workspace
  ON hypothesis_evidence_revisions(workspace_id,hypothesis_id,lifecycle);
"""


class HypothesisError(ValueError):
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
        raise HypothesisError("unauthorized", f"missing required scope {required}")


def _bounded_number(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise HypothesisError("invalid_weight", f"{name} must be between 0 and 1")
    return number


def _normalize_hypotheses(
    values: Sequence[Mapping[str, Any]], *, workspace_seed: str
) -> list[dict[str, Any]]:
    if not 1 <= len(values) <= 100:
        raise HypothesisError(
            "invalid_hypotheses", "one to 100 hypotheses are required"
        )
    normalized = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        statement = " ".join(str(raw.get("statement") or "").split())
        if not statement:
            raise HypothesisError(
                "invalid_hypothesis", "hypothesis statement is required"
            )
        hypothesis_id = str(raw.get("hypothesis_id") or "") or (
            "hypothesis:" + _digest([workspace_seed, index, statement])[:20]
        )
        if hypothesis_id in seen:
            raise HypothesisError(
                "duplicate_hypothesis", "hypothesis identities must be unique"
            )
        seen.add(hypothesis_id)
        predictions = []
        for prediction_index, prediction in enumerate(raw.get("predictions") or []):
            if isinstance(prediction, str):
                prediction = {"statement": prediction}
            prediction = dict(prediction)
            prediction_text = " ".join(str(prediction.get("statement") or "").split())
            if not prediction_text:
                raise HypothesisError(
                    "invalid_prediction", "prediction statement is required"
                )
            predictions.append(
                {
                    "prediction_id": str(prediction.get("prediction_id") or "")
                    or "prediction:"
                    + _digest([hypothesis_id, prediction_index, prediction_text])[:20],
                    "statement": prediction_text,
                    "discriminates_from": sorted(
                        {
                            str(item)
                            for item in prediction.get("discriminates_from") or []
                        }
                    ),
                    "test": dict(prediction.get("test") or {}),
                }
            )
        normalized.append(
            {
                "hypothesis_id": hypothesis_id,
                "label": str(raw.get("label") or f"H{index + 1}"),
                "statement": statement,
                "assumptions": [str(item) for item in raw.get("assumptions") or []],
                "predictions": predictions,
                "alternative_to": sorted(
                    {str(item) for item in raw.get("alternative_to") or []}
                ),
                "status": str(raw.get("status") or "open"),
            }
        )
    return normalized


class HypothesisStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(
        self,
        namespace: str,
        workspace_id: str,
        operation: str,
        object_id: str,
        principal_id: str,
        detail: Mapping[str, Any],
        now: int,
    ) -> None:
        audit_id = (
            "hypothesis-audit:"
            + _digest(
                [
                    namespace,
                    workspace_id,
                    operation,
                    object_id,
                    principal_id,
                    detail,
                    now,
                ]
            )[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO hypothesis_audit VALUES (?,?,?,?,?,?,?,?)",
            [
                audit_id,
                namespace,
                workspace_id,
                operation,
                object_id,
                principal_id,
                _canonical(detail),
                now,
            ],
        )

    def create(
        self,
        namespace: str,
        title: str,
        hypotheses: Sequence[Mapping[str, Any]],
        *,
        principal_id: str,
        scopes: set[str],
        workspace_id: str | None = None,
        idempotency_key: str | None = None,
        parent_workspace_id: str | None = None,
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if not namespace or not title.strip() or generation < 0:
            raise HypothesisError(
                "invalid_workspace", "namespace, title, and generation are required"
            )
        if (
            valid_from_ms is not None
            and valid_to_ms is not None
            and valid_to_ms < valid_from_ms
        ):
            raise HypothesisError(
                "invalid_temporality", "valid-time interval is reversed"
            )
        key = idempotency_key or _digest([title, hypotheses])
        existing = self.conn.execute(
            "SELECT workspace_id FROM hypothesis_workspaces WHERE namespace=? AND idempotency_key=?",
            [namespace, key],
        ).fetchone()
        if existing:
            return {
                **self.get(namespace, existing[0], scopes={READ_SCOPE}),
                "idempotent": True,
            }
        workspace_id = (
            workspace_id or "hypothesis-workspace:" + _digest([namespace, key])[:24]
        )
        normalized = _normalize_hypotheses(hypotheses, workspace_seed=workspace_id)
        now = self.now()
        context = {
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
            "producer": dict(
                producer or {"name": "noesis-hypothesis-workbench", "version": "1.0.0"}
            ),
            "policy": dict(policy or {"comparison": "evidence-honest-v1"}),
        }
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO hypothesis_workspaces VALUES (?,?,?,?,?,?)",
                [workspace_id, namespace, principal_id, now, parent_workspace_id, key],
            )
            result = self._write_revision(
                workspace_id,
                namespace,
                title.strip(),
                "draft",
                normalized,
                principal_id=principal_id,
                predecessor=None,
                revision=1,
                context=context,
                now=now,
            )
            self._audit(
                namespace,
                workspace_id,
                "create",
                result["revision_id"],
                principal_id,
                {},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(namespace, workspace_id, scopes={READ_SCOPE})

    def _write_revision(
        self,
        workspace_id: str,
        namespace: str,
        title: str,
        lifecycle: str,
        hypotheses: Sequence[Mapping[str, Any]],
        *,
        principal_id: str,
        predecessor: str | None,
        revision: int,
        context: Mapping[str, Any],
        now: int,
    ) -> dict[str, Any]:
        stable = {
            "workspace_id": workspace_id,
            "revision": revision,
            "title": title,
            "lifecycle": lifecycle,
            "hypotheses": hypotheses,
            **context,
        }
        input_hash = _digest(stable)
        revision_id = "hypothesis-revision:" + _digest(stable)[:24]
        self.conn.execute(
            "INSERT INTO hypothesis_workspace_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                revision_id,
                workspace_id,
                namespace,
                revision,
                predecessor,
                title,
                lifecycle,
                _canonical(list(hypotheses)),
                int(context["generation"]),
                context["valid_from_ms"],
                context["valid_to_ms"],
                context["observed_at_ms"],
                _canonical(context["producer"]),
                _canonical(context["policy"]),
                principal_id,
                input_hash,
                now,
            ],
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO hypothesis_workspace_current VALUES (?,?,?)",
            [workspace_id, revision_id, revision],
        )
        return {"revision_id": revision_id, "input_hash": input_hash}

    def get(
        self,
        namespace: str,
        workspace_id: str,
        *,
        scopes: set[str],
        include_history: bool = False,
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT r.revision_id,r.revision,r.predecessor_revision_id,r.title,r.lifecycle,"
            "r.hypotheses_json,r.generation,r.valid_from_ms,r.valid_to_ms,r.observed_at_ms,"
            "r.producer_json,r.policy_json,r.principal_id,r.input_hash,r.created_at_ms,w.parent_workspace_id "
            "FROM hypothesis_workspace_revisions r JOIN hypothesis_workspaces w USING(workspace_id) "
            "WHERE r.namespace=? AND r.workspace_id=? "
            + (
                "ORDER BY r.revision"
                if include_history
                else "ORDER BY r.revision DESC LIMIT 1"
            ),
            [namespace, workspace_id],
        ).fetchall()
        values = [self._workspace_row(namespace, workspace_id, row) for row in rows]
        if include_history:
            return {
                "workspace_id": workspace_id,
                "namespace": namespace,
                "revisions": values,
            }
        return values[0] if values else None

    @staticmethod
    def _workspace_row(
        namespace: str, workspace_id: str, row: Sequence[Any]
    ) -> dict[str, Any]:
        return {
            "contract": WORKSPACE_CONTRACT,
            "workspace_id": workspace_id,
            "namespace": namespace,
            "revision_id": row[0],
            "revision": int(row[1]),
            "predecessor_revision_id": row[2],
            "title": row[3],
            "lifecycle": row[4],
            "hypotheses": _load(row[5], []),
            "generation": int(row[6]),
            "valid_from_ms": row[7],
            "valid_to_ms": row[8],
            "observed_at_ms": int(row[9]),
            "producer": _load(row[10], {}),
            "policy": _load(row[11], {}),
            "principal_id": row[12],
            "input_hash": row[13],
            "created_at_ms": int(row[14]),
            "parent_workspace_id": row[15],
        }

    def revise(
        self,
        namespace: str,
        workspace_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        expected_revision: int,
        title: str | None = None,
        hypotheses: Sequence[Mapping[str, Any]] | None = None,
        lifecycle: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        prior = self.get(namespace, workspace_id, scopes={READ_SCOPE})
        if not prior:
            raise HypothesisError("not_found", "workspace does not exist in namespace")
        if prior["revision"] != int(expected_revision):
            raise HypothesisError("revision_conflict", "workspace revision changed")
        next_lifecycle = lifecycle or prior["lifecycle"]
        if next_lifecycle not in WORKSPACE_STATES:
            raise HypothesisError(
                "invalid_lifecycle", "unsupported workspace lifecycle"
            )
        normalized = (
            _normalize_hypotheses(hypotheses, workspace_seed=workspace_id)
            if hypotheses is not None
            else prior["hypotheses"]
        )
        context = {
            key: prior[key]
            for key in (
                "generation",
                "valid_from_ms",
                "valid_to_ms",
                "observed_at_ms",
                "producer",
                "policy",
            )
        }
        stable_candidate = {
            "title": title or prior["title"],
            "lifecycle": next_lifecycle,
            "hypotheses": normalized,
            **context,
        }
        prior_candidate = {
            "title": prior["title"],
            "lifecycle": prior["lifecycle"],
            "hypotheses": prior["hypotheses"],
            **context,
        }
        if _digest(stable_candidate) == _digest(prior_candidate):
            return {**prior, "idempotent": True}
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            written = self._write_revision(
                workspace_id,
                namespace,
                str(title or prior["title"]),
                next_lifecycle,
                normalized,
                principal_id=principal_id,
                predecessor=prior["revision_id"],
                revision=prior["revision"] + 1,
                context=context,
                now=now,
            )
            self._audit(
                namespace,
                workspace_id,
                "revise",
                written["revision_id"],
                principal_id,
                {"from_revision": prior["revision"]},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(namespace, workspace_id, scopes={READ_SCOPE})

    def branch(
        self,
        namespace: str,
        workspace_id: str,
        title: str,
        *,
        principal_id: str,
        scopes: set[str],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        prior = self.get(namespace, workspace_id, scopes={READ_SCOPE})
        if not prior:
            raise HypothesisError("not_found", "workspace does not exist in namespace")
        return self.create(
            namespace,
            title,
            prior["hypotheses"],
            principal_id=principal_id,
            scopes=scopes,
            idempotency_key=idempotency_key
            or f"branch:{workspace_id}:{prior['revision_id']}:{title}",
            parent_workspace_id=workspace_id,
            generation=prior["generation"],
            valid_from_ms=prior["valid_from_ms"],
            valid_to_ms=prior["valid_to_ms"],
            observed_at_ms=prior["observed_at_ms"],
            producer=prior["producer"],
            policy=prior["policy"],
        )

    def link_evidence(
        self,
        namespace: str,
        workspace_id: str,
        hypothesis_id: str,
        evidence_id: str,
        stance: str,
        *,
        principal_id: str,
        scopes: set[str],
        source_revision_id: str | None = None,
        relevance: float = 1.0,
        independence_group: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        annotations: Mapping[str, Any] | None = None,
        required_scope: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        workspace = self.get(namespace, workspace_id, scopes={READ_SCOPE})
        if not workspace:
            raise HypothesisError("not_found", "workspace does not exist in namespace")
        if hypothesis_id not in {
            item["hypothesis_id"] for item in workspace["hypotheses"]
        }:
            raise HypothesisError(
                "unknown_hypothesis", "hypothesis is not in this workspace"
            )
        if stance not in STANCES or not evidence_id:
            raise HypothesisError(
                "invalid_evidence", "evidence identity and stance are required"
            )
        if required_scope and required_scope not in scopes and "operator" not in scopes:
            raise HypothesisError(
                "unauthorized", "cannot link evidence outside the caller's source scope"
            )
        relevance_value = _bounded_number(relevance, "relevance")
        group = str(independence_group or evidence_id)
        link_id = (
            "hypothesis-evidence:"
            + _digest([workspace_id, hypothesis_id, evidence_id])[:24]
        )
        payload = {
            "hypothesis_id": hypothesis_id,
            "evidence_id": evidence_id,
            "source_revision_id": source_revision_id,
            "stance": stance,
            "relevance": relevance_value,
            "independence_group": group,
            "provenance": dict(provenance or {}),
            "annotations": dict(annotations or {}),
            "required_scope": required_scope,
            "lifecycle": "active",
        }
        input_hash = _digest(payload)
        current = self.conn.execute(
            "SELECT c.revision,r.input_hash FROM hypothesis_evidence_current c "
            "JOIN hypothesis_evidence_revisions r USING(link_revision_id) "
            "WHERE c.workspace_id=? AND c.link_id=?",
            [workspace_id, link_id],
        ).fetchone()
        if current and current[1] == input_hash:
            result = self._evidence(
                namespace, workspace_id, scopes=scopes | {READ_SCOPE}
            )
            return {
                **next(item for item in result if item["link_id"] == link_id),
                "idempotent": True,
            }
        revision = int(current[0]) + 1 if current else 1
        now = self.now()
        link_revision_id = (
            "hypothesis-evidence-revision:"
            + _digest([link_id, revision, input_hash])[:24]
        )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO hypothesis_evidence_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    link_revision_id,
                    workspace_id,
                    namespace,
                    link_id,
                    revision,
                    hypothesis_id,
                    evidence_id,
                    source_revision_id,
                    stance,
                    relevance_value,
                    group,
                    _canonical(payload["provenance"]),
                    _canonical(payload["annotations"]),
                    required_scope,
                    "active",
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO hypothesis_evidence_current VALUES (?,?,?,?)",
                [workspace_id, link_id, link_revision_id, revision],
            )
            self._audit(
                namespace,
                workspace_id,
                "link-evidence",
                link_revision_id,
                principal_id,
                {"stance": stance},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return next(
            item
            for item in self._evidence(
                namespace, workspace_id, scopes=scopes | {READ_SCOPE}
            )
            if item["link_id"] == link_id
        )

    def retract_evidence(
        self,
        namespace: str,
        workspace_id: str,
        link_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if len(reason.strip()) < 10:
            raise HypothesisError(
                "invalid_retraction", "a substantive reason is required"
            )
        items = self._evidence(
            namespace,
            workspace_id,
            scopes=scopes | {READ_SCOPE},
            include_retracted=True,
        )
        prior = next((item for item in items if item["link_id"] == link_id), None)
        if not prior:
            raise HypothesisError(
                "not_found", "evidence link does not exist in namespace"
            )
        if prior["lifecycle"] == "retracted":
            return {**prior, "idempotent": True}
        revision = prior["revision"] + 1
        now = self.now()
        annotations = {**prior["annotations"], "retraction_reason": reason}
        payload = {
            key: prior[key]
            for key in (
                "hypothesis_id",
                "evidence_id",
                "source_revision_id",
                "stance",
                "relevance",
                "independence_group",
                "provenance",
                "required_scope",
            )
        }
        payload.update({"annotations": annotations, "lifecycle": "retracted"})
        input_hash = _digest(payload)
        link_revision_id = (
            "hypothesis-evidence-revision:"
            + _digest([link_id, revision, input_hash])[:24]
        )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO hypothesis_evidence_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    link_revision_id,
                    workspace_id,
                    namespace,
                    link_id,
                    revision,
                    prior["hypothesis_id"],
                    prior["evidence_id"],
                    prior["source_revision_id"],
                    prior["stance"],
                    prior["relevance"],
                    prior["independence_group"],
                    _canonical(prior["provenance"]),
                    _canonical(annotations),
                    prior["required_scope"],
                    "retracted",
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO hypothesis_evidence_current VALUES (?,?,?,?)",
                [workspace_id, link_id, link_revision_id, revision],
            )
            self._audit(
                namespace,
                workspace_id,
                "retract-evidence",
                link_revision_id,
                principal_id,
                {"reason": reason},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return next(
            item
            for item in self._evidence(
                namespace,
                workspace_id,
                scopes=scopes | {READ_SCOPE},
                include_retracted=True,
            )
            if item["link_id"] == link_id
        )

    def _evidence(
        self,
        namespace: str,
        workspace_id: str,
        *,
        scopes: set[str],
        include_retracted: bool = False,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT r.link_revision_id,r.link_id,r.revision,r.hypothesis_id,r.evidence_id,"
            "r.source_revision_id,r.stance,r.relevance,r.independence_group,r.provenance_json,"
            "r.annotations_json,r.required_scope,r.lifecycle,r.principal_id,r.input_hash,r.created_at_ms "
            + (
                "FROM hypothesis_evidence_revisions r "
                if include_history
                else "FROM hypothesis_evidence_current c JOIN hypothesis_evidence_revisions r USING(link_revision_id) "
            )
            + "WHERE r.namespace=? AND r.workspace_id=? ORDER BY r.link_id,r.revision",
            [namespace, workspace_id],
        ).fetchall()
        return [
            {
                "link_revision_id": row[0],
                "link_id": row[1],
                "revision": int(row[2]),
                "hypothesis_id": row[3],
                "evidence_id": row[4],
                "source_revision_id": row[5],
                "stance": row[6],
                "relevance": float(row[7]),
                "independence_group": row[8],
                "provenance": _load(row[9], {}),
                "annotations": _load(row[10], {}),
                "required_scope": row[11],
                "lifecycle": row[12],
                "principal_id": row[13],
                "input_hash": row[14],
                "created_at_ms": int(row[15]),
            }
            for row in rows
            if (include_retracted or row[12] == "active")
            and (not row[11] or row[11] in scopes or "operator" in scopes)
        ]

    def compare(
        self,
        namespace: str,
        workspace_id: str,
        *,
        scopes: set[str],
        method: str = "qualitative",
        priors: Mapping[str, float] | None = None,
        sensitivity: float = 0.15,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        workspace = self.get(namespace, workspace_id, scopes=scopes)
        if not workspace:
            raise HypothesisError("not_found", "workspace does not exist in namespace")
        if method not in {"qualitative", "weighted"}:
            raise HypothesisError("invalid_method", "comparison method is unsupported")
        sensitivity = _bounded_number(sensitivity, "sensitivity")
        all_rows = self.conn.execute(
            "SELECT required_scope,lifecycle FROM hypothesis_evidence_current c "
            "JOIN hypothesis_evidence_revisions r USING(link_revision_id) "
            "WHERE r.namespace=? AND r.workspace_id=?",
            [namespace, workspace_id],
        ).fetchall()
        evidence = self._evidence(namespace, workspace_id, scopes=scopes)
        inaccessible = sum(
            1
            for required, lifecycle in all_rows
            if lifecycle == "active"
            and required
            and required not in scopes
            and "operator" not in scopes
        )
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in evidence:
            grouped[(item["hypothesis_id"], item["independence_group"])].append(item)
        prior_values = dict(priors or {})
        results = []
        rank_scores: dict[str, float] = {}
        for hypothesis in workspace["hypotheses"]:
            hypothesis_id = hypothesis["hypothesis_id"]
            prior = _bounded_number(prior_values.get(hypothesis_id, 0.5), "prior")
            support = contradiction = ambiguity = 0.0
            group_count = 0
            for (owner, _), items in grouped.items():
                if owner != hypothesis_id:
                    continue
                group_count += 1
                best = max(items, key=lambda item: item["relevance"])
                if best["stance"] == "support":
                    support += best["relevance"]
                elif best["stance"] == "contradict":
                    contradiction += best["relevance"]
                else:
                    ambiguity += best["relevance"]
            raw = prior - 0.5 + support - contradiction
            width = min(
                1.0, sensitivity + ambiguity * 0.1 + (0.35 if group_count < 2 else 0.0)
            )
            score = max(-1.0, min(1.0, raw))
            rank_scores[hypothesis_id] = score
            if group_count == 0:
                assessment = "insufficient"
            elif score > sensitivity:
                assessment = "favored"
            elif score < -sensitivity:
                assessment = "disfavored"
            else:
                assessment = "mixed"
            results.append(
                {
                    "hypothesis_id": hypothesis_id,
                    "assessment": assessment,
                    "score": round(score, 3) if method == "weighted" else None,
                    "interval": [
                        round(max(-1.0, score - width), 3),
                        round(min(1.0, score + width), 3),
                    ],
                    "support": round(support, 3),
                    "contradiction": round(contradiction, 3),
                    "ambiguity": round(ambiguity, 3),
                    "independent_groups": group_count,
                    "prior": prior if priors else None,
                }
            )
        ranking = sorted(rank_scores, key=lambda item: (-rank_scores[item], item))
        tie = (
            len(ranking) > 1
            and abs(rank_scores[ranking[0]] - rank_scores[ranking[1]]) <= sensitivity
        )
        return {
            "contract": COMPARISON_CONTRACT,
            "workspace_id": workspace_id,
            "workspace_revision_id": workspace["revision_id"],
            "method": method,
            "results": results,
            "ranking": ranking,
            "tie": tie,
            "sensitivity": sensitivity,
            "inaccessible_evidence_count": inaccessible,
            "limitations": [
                "scores are comparison aids, not posterior truth probabilities",
                "correlated evidence is counted once per declared independence group",
            ],
            "comparison_hash": _digest(
                [workspace["revision_id"], method, results, sensitivity]
            ),
        }

    def create_plan(
        self,
        namespace: str,
        workspace_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        max_steps: int = 25,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        workspace = self.get(namespace, workspace_id, scopes={READ_SCOPE})
        if not workspace:
            raise HypothesisError("not_found", "workspace does not exist in namespace")
        steps = []
        for hypothesis in workspace["hypotheses"]:
            for prediction in hypothesis["predictions"]:
                steps.append(
                    {
                        "step_id": "hypothesis-step:"
                        + _digest(
                            [
                                workspace["revision_id"],
                                hypothesis["hypothesis_id"],
                                prediction["prediction_id"],
                            ]
                        )[:20],
                        "hypothesis_id": hypothesis["hypothesis_id"],
                        "prediction_id": prediction["prediction_id"],
                        "query": prediction["statement"],
                        "discriminates_from": prediction["discriminates_from"],
                        "cost": float(prediction["test"].get("cost", 1.0)),
                    }
                )
        steps = steps[: min(max(1, int(max_steps)), 100)]
        gaps = (
            []
            if steps
            else [
                {"kind": "missing-discriminating-predictions", "status": "unresolved"}
            ]
        )
        plan_hash = _digest([workspace["revision_id"], steps])
        plan_id = "hypothesis-plan:" + plan_hash[:24]
        existing = self.conn.execute(
            "SELECT plan_id FROM hypothesis_research_plans WHERE plan_id=?", [plan_id]
        ).fetchone()
        if existing:
            return {
                **self.get_plan(namespace, plan_id, scopes={READ_SCOPE}),
                "idempotent": True,
            }
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO hypothesis_research_plans VALUES (?,?,?,?,?,0,'[]',?,'ready',0,?,?,?,?)",
                [
                    plan_id,
                    workspace_id,
                    namespace,
                    workspace["revision_id"],
                    _canonical(steps),
                    _canonical(gaps),
                    plan_hash,
                    principal_id,
                    now,
                    now,
                ],
            )
            self._audit(
                namespace,
                workspace_id,
                "create-plan",
                plan_id,
                principal_id,
                {"step_count": len(steps)},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get_plan(namespace, plan_id, scopes={READ_SCOPE})

    def get_plan(
        self, namespace: str, plan_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT workspace_id,workspace_revision_id,steps_json,cursor,results_json,gaps_json,"
            "status,budget_used,plan_hash,principal_id,created_at_ms,updated_at_ms "
            "FROM hypothesis_research_plans WHERE namespace=? AND plan_id=?",
            [namespace, plan_id],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": PLAN_CONTRACT,
            "plan_id": plan_id,
            "workspace_id": row[0],
            "namespace": namespace,
            "workspace_revision_id": row[1],
            "steps": _load(row[2], []),
            "cursor": int(row[3]),
            "results": _load(row[4], []),
            "gaps": _load(row[5], []),
            "status": row[6],
            "budget_used": float(row[7]),
            "plan_hash": row[8],
            "principal_id": row[9],
            "created_at_ms": int(row[10]),
            "updated_at_ms": int(row[11]),
        }

    def execute_plan(
        self,
        namespace: str,
        plan_id: str,
        observations: Sequence[Mapping[str, Any]],
        *,
        principal_id: str,
        scopes: set[str],
        budget: float,
        cancel_requested: bool = False,
    ) -> dict[str, Any]:
        _require(scopes, EXECUTE_SCOPE)
        plan = self.get_plan(namespace, plan_id, scopes={READ_SCOPE})
        if not plan:
            raise HypothesisError(
                "not_found", "research plan does not exist in namespace"
            )
        if plan["status"] == "complete":
            return {**plan, "idempotent": True}
        if not math.isfinite(float(budget)) or budget < 0:
            raise HypothesisError(
                "invalid_budget", "budget must be a finite non-negative number"
            )
        by_step = {str(item.get("step_id")): dict(item) for item in observations}
        cursor, used, results = (
            plan["cursor"],
            plan["budget_used"],
            list(plan["results"]),
        )
        status = "running"
        gaps = list(plan["gaps"])
        if cancel_requested:
            status = "cancelled"
        else:
            while cursor < len(plan["steps"]):
                step = plan["steps"][cursor]
                cost = max(0.0, float(step["cost"]))
                if used + cost > budget:
                    status = "paused"
                    gap = {
                        "kind": "budget-exhausted",
                        "step_id": step["step_id"],
                        "status": "unresolved",
                    }
                    if gap not in gaps:
                        gaps.append(gap)
                    break
                observation = by_step.get(step["step_id"])
                if observation is None:
                    status = "paused"
                    gap = {
                        "kind": "source-unavailable",
                        "step_id": step["step_id"],
                        "status": "unresolved",
                    }
                    if gap not in gaps:
                        gaps.append(gap)
                    break
                gaps = [
                    {**gap, "status": "resolved"}
                    if gap.get("step_id") == step["step_id"]
                    else gap
                    for gap in gaps
                ]
                results.append({"step_id": step["step_id"], "observation": observation})
                used += cost
                cursor += 1
            else:
                status = "complete"
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "UPDATE hypothesis_research_plans SET cursor=?,results_json=?,gaps_json=?,status=?,"
                "budget_used=?,principal_id=?,updated_at_ms=? WHERE namespace=? AND plan_id=?",
                [
                    cursor,
                    _canonical(results),
                    _canonical(gaps),
                    status,
                    used,
                    principal_id,
                    now,
                    namespace,
                    plan_id,
                ],
            )
            self._audit(
                namespace,
                plan["workspace_id"],
                "execute-plan",
                plan_id,
                principal_id,
                {"cursor": cursor, "status": status, "budget_used": used},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get_plan(namespace, plan_id, scopes={READ_SCOPE})

    def export(
        self, namespace: str, workspace_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        workspace = self.get(
            namespace, workspace_id, scopes=scopes, include_history=True
        )
        if not workspace or not workspace["revisions"]:
            raise HypothesisError("not_found", "workspace does not exist in namespace")
        evidence = self._evidence(
            namespace,
            workspace_id,
            scopes=scopes,
            include_retracted=True,
            include_history=True,
        )
        total_evidence = self.conn.execute(
            "SELECT COUNT(*) FROM hypothesis_evidence_revisions WHERE namespace=? AND workspace_id=?",
            [namespace, workspace_id],
        ).fetchone()[0]
        plans = self.conn.execute(
            "SELECT plan_id FROM hypothesis_research_plans WHERE namespace=? AND workspace_id=? ORDER BY plan_id",
            [namespace, workspace_id],
        ).fetchall()
        audits = self.conn.execute(
            "SELECT audit_id,operation,object_id,principal_id,detail_json,created_at_ms "
            "FROM hypothesis_audit WHERE namespace=? AND workspace_id=? "
            "ORDER BY created_at_ms,audit_id",
            [namespace, workspace_id],
        ).fetchall()
        payload = {
            "contract": EXPORT_CONTRACT,
            "namespace": namespace,
            "workspace_id": workspace_id,
            "workspace_history": workspace["revisions"],
            "evidence": evidence,
            "research_plans": [
                self.get_plan(namespace, row[0], scopes=scopes) for row in plans
            ],
            "omissions": {
                "inaccessible_evidence_revisions": int(total_evidence) - len(evidence)
            },
            "audit": [
                {
                    "audit_id": row[0],
                    "operation": row[1],
                    "object_id": row[2],
                    "principal_id": row[3],
                    "detail": _load(row[4], {}),
                    "created_at_ms": int(row[5]),
                }
                for row in audits
            ],
        }
        return {**payload, "export_hash": _digest(payload)}

    def replay(
        self, namespace: str, workspace_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        exported = self.export(namespace, workspace_id, scopes=scopes)
        replayed_hash = _digest(
            {key: value for key, value in exported.items() if key != "export_hash"}
        )
        return {
            "workspace_id": workspace_id,
            "export_hash": exported["export_hash"],
            "replayed_hash": replayed_hash,
            "deterministic": replayed_hash == exported["export_hash"],
            "revision_count": len(exported["workspace_history"]),
            "evidence_count": len(exported["evidence"]),
        }
