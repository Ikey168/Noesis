"""Explainable source registry, constrained planning, and checkpointed execution."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

CAPABILITY_CONTRACT = "noesis-source-capability-v1"
OBJECTIVE_CONTRACT = "noesis-source-research-objective-v1"
PLAN_CONTRACT = "noesis-source-acquisition-plan-v1"
RECEIPT_CONTRACT = "noesis-source-plan-receipt-v1"
READ_SCOPE = "knowledge:source-planner:read"
WRITE_SCOPE = "knowledge:source-planner:write"
EXECUTE_SCOPE = "knowledge:source-planner:execute"
_SENSITIVE_KEYS = {"secret", "token", "password", "api_key", "apikey", "authorization"}

_DDL = """
CREATE TABLE IF NOT EXISTS source_capability_versions (
  capability_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, source_id TEXT NOT NULL,
  semantic_version TEXT NOT NULL, coverage_json TEXT NOT NULL, authority_json TEXT NOT NULL,
  access_json TEXT NOT NULL, latency_json TEXT NOT NULL, cost_json TEXT NOT NULL,
  rate_limits_json TEXT NOT NULL, query_forms_json TEXT NOT NULL, connector_json TEXT NOT NULL,
  dependency_group TEXT NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL,
  supersedes_capability_id TEXT, generation BIGINT NOT NULL, valid_from_ms BIGINT,
  valid_to_ms BIGINT, observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL,
  policy_json TEXT NOT NULL, provenance_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(namespace,source_id,semantic_version)
);
CREATE TABLE IF NOT EXISTS source_capability_current (
  namespace TEXT NOT NULL, source_id TEXT NOT NULL, capability_id TEXT NOT NULL,
  PRIMARY KEY(namespace,source_id)
);
CREATE TABLE IF NOT EXISTS source_research_objectives (
  objective_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, question TEXT NOT NULL,
  decomposition_json TEXT NOT NULL, evidence_classes_json TEXT NOT NULL,
  constraints_json TEXT NOT NULL, generation BIGINT NOT NULL, valid_from_ms BIGINT,
  valid_to_ms BIGINT, observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL,
  policy_json TEXT NOT NULL, provenance_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  input_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_acquisition_plans (
  plan_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, objective_id TEXT NOT NULL,
  plan_json TEXT NOT NULL, plan_hash TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(namespace,objective_id,plan_hash)
);
CREATE TABLE IF NOT EXISTS source_plan_runs (
  run_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, plan_id TEXT NOT NULL,
  execution_key TEXT NOT NULL, status TEXT NOT NULL, budget_spent DOUBLE NOT NULL,
  receipt_json TEXT, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  updated_at_ms BIGINT NOT NULL, UNIQUE(namespace,plan_id,execution_key)
);
CREATE TABLE IF NOT EXISTS source_plan_checkpoints (
  run_id TEXT NOT NULL, step_id TEXT NOT NULL, source_id TEXT NOT NULL,
  capability_id TEXT NOT NULL, status TEXT NOT NULL, attempt BIGINT NOT NULL,
  cursor_json TEXT NOT NULL, cost DOUBLE NOT NULL, receipt_json TEXT NOT NULL,
  updated_at_ms BIGINT NOT NULL, PRIMARY KEY(run_id,step_id)
);
CREATE TABLE IF NOT EXISTS source_planner_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
"""


class SourcePlannerError(ValueError):
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


def source_plan_run_id(namespace: str, plan_id: str, execution_key: str) -> str:
    """Return the stable run identity used by execution and cancellation."""
    return "source-plan-run:" + _digest([namespace, plan_id, execution_key])[:24]


def _load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise SourcePlannerError("unauthorized", f"missing required scope {required}")


def _reject_secrets(value: Any, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS:
                raise SourcePlannerError(
                    "embedded_secret",
                    f"credential material is forbidden at {path}.{key}",
                )
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")


class SourcePlannerStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(self, namespace, operation, object_id, principal_id, detail, now):
        audit_id = (
            "source-planner-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO source_planner_audit VALUES (?,?,?,?,?,?,?)",
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

    def register_capability(
        self,
        namespace: str,
        source_id: str,
        semantic_version: str,
        *,
        coverage: Mapping[str, Any],
        authority: Mapping[str, Any],
        access: Mapping[str, Any],
        latency: Mapping[str, Any],
        cost: Mapping[str, Any],
        rate_limits: Mapping[str, Any],
        query_forms: Sequence[str],
        connector: Mapping[str, Any],
        dependency_group: str,
        principal_id: str,
        scopes: set[str],
        supersedes_capability_id: str | None = None,
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if not all(
            str(item).strip()
            for item in (namespace, source_id, semantic_version, dependency_group)
        ):
            raise SourcePlannerError(
                "invalid_capability",
                "namespace, source, version, and dependency group are required",
            )
        query_values = sorted(
            {str(item).strip() for item in query_forms if str(item).strip()}
        )
        if not query_values:
            raise SourcePlannerError(
                "invalid_capability", "at least one query form is required"
            )
        values = {
            "coverage": dict(coverage),
            "authority": dict(authority),
            "access": dict(access),
            "latency": dict(latency),
            "cost": dict(cost),
            "rate_limits": dict(rate_limits),
            "query_forms": query_values,
            "connector": dict(connector),
        }
        _reject_secrets(values)
        authority_score = float(values["authority"].get("score", 0))
        if not 0 <= authority_score <= 1:
            raise SourcePlannerError(
                "invalid_capability", "authority score must be between zero and one"
            )
        if (
            float(values["cost"].get("per_query", 0)) < 0
            or int(values["latency"].get("p95_ms", 0)) < 0
        ):
            raise SourcePlannerError(
                "invalid_capability", "cost and latency must be non-negative"
            )
        now = self.now()
        stable = {
            "namespace": namespace,
            "source_id": source_id,
            "semantic_version": semantic_version,
            **values,
            "dependency_group": dependency_group,
            "supersedes_capability_id": supersedes_capability_id,
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
            "producer": dict(
                producer or {"name": "noesis-source-planner", "version": "1.0.0"}
            ),
            "policy": dict(policy or {}),
            "provenance": dict(provenance or {}),
        }
        content_hash = _digest(
            {k: v for k, v in stable.items() if k != "observed_at_ms"}
        )
        capability_id = (
            "source-capability:"
            + _digest([namespace, source_id, semantic_version])[:24]
        )
        existing = self.conn.execute(
            "SELECT content_hash FROM source_capability_versions WHERE capability_id=?",
            [capability_id],
        ).fetchone()
        if existing:
            if existing[0] != content_hash:
                raise SourcePlannerError(
                    "immutable_version",
                    "source capability version has different content",
                )
            return {
                **self.capability(namespace, capability_id, scopes={READ_SCOPE}),
                "idempotent": True,
            }
        current = self.conn.execute(
            "SELECT capability_id FROM source_capability_current WHERE namespace=? AND source_id=?",
            [namespace, source_id],
        ).fetchone()
        if current and supersedes_capability_id != current[0]:
            raise SourcePlannerError(
                "version_conflict",
                "a new source version must supersede the current capability",
            )
        if supersedes_capability_id and not self.capability(
            namespace, supersedes_capability_id, scopes={READ_SCOPE}
        ):
            raise SourcePlannerError(
                "capability_not_found", "superseded capability does not exist"
            )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO source_capability_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    capability_id,
                    namespace,
                    source_id,
                    semantic_version,
                    _canonical(values["coverage"]),
                    _canonical(values["authority"]),
                    _canonical(values["access"]),
                    _canonical(values["latency"]),
                    _canonical(values["cost"]),
                    _canonical(values["rate_limits"]),
                    _canonical(query_values),
                    _canonical(values["connector"]),
                    dependency_group,
                    content_hash,
                    "active",
                    supersedes_capability_id,
                    generation,
                    valid_from_ms,
                    valid_to_ms,
                    stable["observed_at_ms"],
                    _canonical(stable["producer"]),
                    _canonical(stable["policy"]),
                    _canonical(stable["provenance"]),
                    principal_id,
                    now,
                ],
            )
            if supersedes_capability_id:
                self.conn.execute(
                    "UPDATE source_capability_versions SET status='superseded' WHERE namespace=? AND capability_id=?",
                    [namespace, supersedes_capability_id],
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO source_capability_current VALUES (?,?,?)",
                [namespace, source_id, capability_id],
            )
            self._audit(
                namespace, "register-capability", capability_id, principal_id, {}, now
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.capability(namespace, capability_id, scopes={READ_SCOPE})

    def capability(
        self, namespace: str, capability_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT source_id,semantic_version,coverage_json,authority_json,access_json,latency_json,cost_json,rate_limits_json,query_forms_json,connector_json,dependency_group,content_hash,status,supersedes_capability_id,generation,valid_from_ms,valid_to_ms,observed_at_ms,producer_json,policy_json,provenance_json,principal_id,created_at_ms FROM source_capability_versions WHERE namespace=? AND capability_id=?",
            [namespace, capability_id],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": CAPABILITY_CONTRACT,
            "capability_id": capability_id,
            "namespace": namespace,
            "source_id": row[0],
            "semantic_version": row[1],
            "coverage": _load(row[2], {}),
            "authority": _load(row[3], {}),
            "access": _load(row[4], {}),
            "latency": _load(row[5], {}),
            "cost": _load(row[6], {}),
            "rate_limits": _load(row[7], {}),
            "query_forms": _load(row[8], []),
            "connector": _load(row[9], {}),
            "dependency_group": row[10],
            "content_hash": row[11],
            "status": row[12],
            "supersedes_capability_id": row[13],
            "generation": int(row[14]),
            "valid_from_ms": row[15],
            "valid_to_ms": row[16],
            "observed_at_ms": int(row[17]),
            "producer": _load(row[18], {}),
            "policy": _load(row[19], {}),
            "provenance": _load(row[20], {}),
            "principal_id": row[21],
            "created_at_ms": int(row[22]),
        }

    def create_objective(
        self,
        namespace: str,
        question: str,
        decomposition: Sequence[Mapping[str, Any]],
        evidence_classes: Sequence[str],
        constraints: Mapping[str, Any] | None,
        *,
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
        parts = [dict(item) for item in decomposition] or [
            {"question": question, "query_form": "search"}
        ]
        classes = sorted({str(item) for item in evidence_classes}) or ["secondary"]
        controls = {
            "domain": "*",
            "min_independence": 1,
            "max_capability_age_ms": None,
            "budget": 10.0,
            "max_sources": 10,
            "max_results": 100,
            "max_pages": 5,
            "timeout_ms": 30_000,
            "retries": 1,
            "redistribute": False,
            "required_sources": [],
            "forbidden_sources": [],
            "allowed_licenses": [],
            **dict(constraints or {}),
        }
        if not namespace.strip() or not question.strip():
            raise SourcePlannerError(
                "invalid_objective", "namespace and question are required"
            )
        if set(controls["required_sources"]) & set(controls["forbidden_sources"]):
            raise SourcePlannerError(
                "conflicting_constraints",
                "a source cannot be both required and forbidden",
            )
        if float(controls["budget"]) < 0 or int(controls["min_independence"]) < 1:
            raise SourcePlannerError(
                "invalid_objective", "budget and independence constraints are invalid"
            )
        for part in parts:
            if (
                not str(part.get("question", "")).strip()
                or not str(part.get("query_form", "")).strip()
            ):
                raise SourcePlannerError(
                    "invalid_objective",
                    "every decomposition item requires question and query form",
                )
        now = self.now()
        stable = {
            "namespace": namespace,
            "question": question.strip(),
            "decomposition": parts,
            "evidence_classes": classes,
            "constraints": controls,
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
            "producer": dict(
                producer or {"name": "noesis-source-planner", "version": "1.0.0"}
            ),
            "policy": dict(policy or {}),
            "provenance": dict(provenance or {}),
        }
        _reject_secrets(stable)
        input_hash = _digest({k: v for k, v in stable.items() if k != "observed_at_ms"})
        objective_id = "source-objective:" + input_hash[:24]
        existing = self.conn.execute(
            "SELECT created_at_ms FROM source_research_objectives WHERE objective_id=?",
            [objective_id],
        ).fetchone()
        if not existing:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    "INSERT INTO source_research_objectives VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        objective_id,
                        namespace,
                        stable["question"],
                        _canonical(parts),
                        _canonical(classes),
                        _canonical(controls),
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
                self._audit(
                    namespace, "create-objective", objective_id, principal_id, {}, now
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {
            "contract": OBJECTIVE_CONTRACT,
            "objective_id": objective_id,
            **stable,
            "input_hash": input_hash,
            "created_at_ms": int(existing[0]) if existing else now,
            "idempotent": bool(existing),
        }

    def objective(
        self, namespace: str, objective_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT question,decomposition_json,evidence_classes_json,constraints_json,generation,valid_from_ms,valid_to_ms,observed_at_ms,producer_json,policy_json,provenance_json,input_hash,created_at_ms FROM source_research_objectives WHERE namespace=? AND objective_id=?",
            [namespace, objective_id],
        ).fetchone()
        if not row:
            raise SourcePlannerError(
                "objective_not_found", "research objective does not exist"
            )
        return {
            "contract": OBJECTIVE_CONTRACT,
            "objective_id": objective_id,
            "namespace": namespace,
            "question": row[0],
            "decomposition": _load(row[1], []),
            "evidence_classes": _load(row[2], []),
            "constraints": _load(row[3], {}),
            "generation": int(row[4]),
            "valid_from_ms": row[5],
            "valid_to_ms": row[6],
            "observed_at_ms": int(row[7]),
            "producer": _load(row[8], {}),
            "policy": _load(row[9], {}),
            "provenance": _load(row[10], {}),
            "input_hash": row[11],
            "created_at_ms": int(row[12]),
        }

    def _capabilities(self, namespace: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT capability_id FROM source_capability_current WHERE namespace=? ORDER BY source_id",
            [namespace],
        ).fetchall()
        return [self.capability(namespace, row[0], scopes={READ_SCOPE}) for row in rows]

    def preview(
        self,
        namespace: str,
        objective_id: str,
        *,
        scopes: set[str],
        at_ms: int,
        credential_available: Callable[[str], bool] | None = None,
        persist: bool = False,
        principal_id: str = "preview",
        optimizer: str = "greedy",
        solver_timeout_seconds: float = 2,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE if persist else READ_SCOPE)
        objective = self.objective(namespace, objective_id, scopes={READ_SCOPE})
        constraints = objective["constraints"]
        required = set(constraints["required_sources"])
        forbidden = set(constraints["forbidden_sources"])
        candidates, exclusions = [], []
        for capability in self._capabilities(namespace):
            reasons = []
            source_id = capability["source_id"]
            coverage = capability["coverage"]
            access = capability["access"]
            if source_id in forbidden:
                reasons.append("forbidden-source")
            domains = set(coverage.get("domains", ["*"]))
            if constraints["domain"] not in domains and "*" not in domains:
                reasons.append("domain-not-covered")
            if not set(objective["evidence_classes"]) & set(
                coverage.get("evidence_classes", [])
            ):
                reasons.append("evidence-class-not-covered")
            query_forms = {item["query_form"] for item in objective["decomposition"]}
            if not query_forms <= set(capability["query_forms"]):
                reasons.append("query-form-not-supported")
            credential_ref = str(access.get("credential_ref", ""))
            credential_ready = not access.get("credential_required", False) or bool(
                credential_ref
                and credential_available
                and credential_available(credential_ref)
            )
            if not credential_ready:
                reasons.append("credential-missing")
            if not access.get("terms_accepted", True):
                reasons.append("license-not-accepted")
            if constraints["redistribute"] and not access.get("redistribution", False):
                reasons.append("redistribution-forbidden")
            allowed = set(constraints["allowed_licenses"])
            if allowed and access.get("license_id") not in allowed:
                reasons.append("license-not-allowed")
            if access.get("outage", False) or (
                access.get("available_after_ms") is not None
                and at_ms < int(access["available_after_ms"])
            ):
                reasons.append("source-unavailable")
            age = at_ms - capability["observed_at_ms"]
            maximum_age = constraints.get("max_capability_age_ms")
            if maximum_age is not None and age > int(maximum_age):
                reasons.append("stale-capability")
            if reasons:
                exclusions.append(
                    {
                        "source_id": source_id,
                        "capability_id": capability["capability_id"],
                        "reasons": sorted(set(reasons)),
                        "required": source_id in required,
                    }
                )
                continue
            covered_parts = [
                index
                for index, part in enumerate(objective["decomposition"])
                if part["query_form"] in capability["query_forms"]
            ]
            per_query = float(capability["cost"].get("per_query", 0))
            projected_cost = per_query * max(1, len(covered_parts))
            authority = float(capability["authority"].get("score", 0))
            latency = float(capability["latency"].get("p95_ms", 0))
            score_components = {
                "coverage": len(covered_parts)
                / max(1, len(objective["decomposition"])),
                "authority": authority,
                "latency": 1 / (1 + latency / 1000),
                "cost": 1 / (1 + projected_cost),
                "freshness": 1 / (1 + max(0, age) / 86_400_000),
            }
            score = round(sum(score_components.values()) / len(score_components), 8)
            candidates.append(
                {
                    "capability": capability,
                    "covered_parts": covered_parts,
                    "projected_cost": projected_cost,
                    "score": score,
                    "score_components": score_components,
                }
            )
        candidates.sort(
            key=lambda item: (-item["score"], item["capability"]["source_id"])
        )
        fallback_candidates = list(candidates)
        if optimizer not in {"greedy", "cp-sat"}:
            raise SourcePlannerError("invalid_optimizer", "unknown source optimizer")
        if optimizer == "cp-sat":
            from src.integrations.planning import select_sources
            optimization = select_sources(candidates, constraints, len(objective["decomposition"]),
                                          timeout_seconds=solver_timeout_seconds)
            constraints = {**constraints, "optimization": optimization}
            if optimization["status"] not in {"OPTIMAL", "FEASIBLE"}:
                raise SourcePlannerError("optimizer_" + optimization["status"].lower(),
                                         "solver did not produce a feasible source plan", optimization=optimization)
            chosen = set(optimization["selected_ids"])
            for item in candidates:
                if item["capability"]["source_id"] not in chosen:
                    exclusions.append({"source_id": item["capability"]["source_id"],
                                       "capability_id": item["capability"]["capability_id"],
                                       "reasons": ["optimizer-not-selected"], "required": False})
            candidates = [item for item in candidates if item["capability"]["source_id"] in chosen]
        selected, spent, groups, covered = [], 0.0, set(), set()
        budget = float(constraints["budget"])
        for item in candidates:
            capability = item["capability"]
            if len(selected) >= int(constraints["max_sources"]):
                break
            if spent + item["projected_cost"] > budget:
                exclusions.append(
                    {
                        "source_id": capability["source_id"],
                        "capability_id": capability["capability_id"],
                        "reasons": ["budget-exceeded"],
                        "required": capability["source_id"] in required,
                    }
                )
                continue
            adds_coverage = bool(set(item["covered_parts"]) - covered)
            adds_independence = capability["dependency_group"] not in groups
            if (
                optimizer == "greedy"
                and selected
                and not adds_coverage
                and not adds_independence
                and capability["source_id"] not in required
            ):
                exclusions.append(
                    {
                        "source_id": capability["source_id"],
                        "capability_id": capability["capability_id"],
                        "reasons": ["redundant-source"],
                        "required": False,
                    }
                )
                continue
            step_id = (
                "source-plan-step:"
                + _digest([objective_id, capability["capability_id"]])[:24]
            )
            queries = [
                {
                    "part": index,
                    "question": objective["decomposition"][index]["question"],
                    "query_form": objective["decomposition"][index]["query_form"],
                    "parameters": dict(
                        objective["decomposition"][index].get("parameters", {})
                    ),
                }
                for index in item["covered_parts"]
            ]
            selected.append(
                {
                    "step_id": step_id,
                    "order": len(selected) + 1,
                    "source_id": capability["source_id"],
                    "capability_id": capability["capability_id"],
                    "connector": capability["connector"],
                    "queries": queries,
                    "projected_cost": item["projected_cost"],
                    "score": item["score"],
                    "score_components": item["score_components"],
                    "dependency_group": capability["dependency_group"],
                }
            )
            spent += item["projected_cost"]
            covered.update(item["covered_parts"])
            groups.add(capability["dependency_group"])
        selected_ids = {item["source_id"] for item in selected}
        fallback_steps = []
        for item in fallback_candidates:
            capability = item["capability"]
            if (
                capability["source_id"] in selected_ids
                or item["projected_cost"] > budget
            ):
                continue
            fallback_steps.append(
                {
                    "step_id": "source-plan-fallback:"
                    + _digest([objective_id, capability["capability_id"]])[:24],
                    "order": len(fallback_steps) + 1,
                    "source_id": capability["source_id"],
                    "capability_id": capability["capability_id"],
                    "connector": capability["connector"],
                    "queries": [
                        {
                            "part": index,
                            "question": objective["decomposition"][index]["question"],
                            "query_form": objective["decomposition"][index][
                                "query_form"
                            ],
                            "parameters": dict(
                                objective["decomposition"][index].get("parameters", {})
                            ),
                        }
                        for index in item["covered_parts"]
                    ],
                    "projected_cost": item["projected_cost"],
                    "score": item["score"],
                    "score_components": item["score_components"],
                    "dependency_group": capability["dependency_group"],
                }
            )
        missing_required = required - selected_ids
        infeasibility = []
        if len(covered) < len(objective["decomposition"]):
            infeasibility.append("question-parts-uncovered")
        if len(groups) < int(constraints["min_independence"]):
            infeasibility.append("independence-unmet")
        if missing_required:
            infeasibility.append("required-source-unavailable")
        canonical = {
            "contract": PLAN_CONTRACT,
            "namespace": namespace,
            "objective_id": objective_id,
            "at_ms": int(at_ms),
            "steps": selected,
            "fallback_steps": fallback_steps,
            "exclusions": sorted(exclusions, key=lambda item: item["source_id"]),
            "coverage": {
                "parts_total": len(objective["decomposition"]),
                "parts_covered": len(covered),
                "independent_groups": len(groups),
            },
            "budget": {"limit": budget, "projected": round(spent, 8)},
            "feasible": not infeasibility,
            "infeasibility": sorted(infeasibility),
            "constraints": constraints,
        }
        plan_hash = _digest(canonical)
        plan_id = "source-plan:" + plan_hash[:24]
        result = {**canonical, "plan_id": plan_id, "plan_hash": plan_hash}
        if persist:
            existing = self.conn.execute(
                "SELECT created_at_ms FROM source_acquisition_plans WHERE plan_id=? AND namespace=?",
                [plan_id, namespace],
            ).fetchone()
            now = self.now()
            if not existing:
                self.conn.execute("BEGIN")
                try:
                    self.conn.execute(
                        "INSERT INTO source_acquisition_plans VALUES (?,?,?,?,?,?,?)",
                        [
                            plan_id,
                            namespace,
                            objective_id,
                            _canonical(result),
                            plan_hash,
                            principal_id,
                            now,
                        ],
                    )
                    self._audit(
                        namespace,
                        "create-plan",
                        plan_id,
                        principal_id,
                        {"feasible": result["feasible"]},
                        now,
                    )
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            result.update(
                {
                    "created_at_ms": int(existing[0]) if existing else now,
                    "idempotent": bool(existing),
                }
            )
        return result

    def plan(self, namespace: str, plan_id: str, *, scopes: set[str]) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT plan_json FROM source_acquisition_plans WHERE namespace=? AND plan_id=?",
            [namespace, plan_id],
        ).fetchone()
        if not row:
            raise SourcePlannerError("plan_not_found", "source plan does not exist")
        return _load(row[0], {})

    def execute(
        self,
        namespace: str,
        plan_id: str,
        execution_key: str,
        *,
        runner: Callable[
            [Mapping[str, Any], Mapping[str, Any], Mapping[str, Any] | None],
            Mapping[str, Any],
        ],
        principal_id: str,
        scopes: set[str],
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, EXECUTE_SCOPE)
        plan = self.plan(namespace, plan_id, scopes={READ_SCOPE})
        if not plan["feasible"]:
            raise SourcePlannerError(
                "plan_infeasible", "infeasible source plan cannot execute"
            )
        run_id = source_plan_run_id(namespace, plan_id, execution_key)
        prior = self.conn.execute(
            "SELECT status,receipt_json FROM source_plan_runs WHERE run_id=? AND namespace=?",
            [run_id, namespace],
        ).fetchone()
        if prior and prior[0] in {"completed", "partial", "cancelled"}:
            return {**_load(prior[1], {}), "idempotent": True}
        now = self.now()
        if not prior:
            self.conn.execute(
                "INSERT INTO source_plan_runs VALUES (?,?,?,?,'running',0,NULL,?,?,?)",
                [run_id, namespace, plan_id, execution_key, principal_id, now, now],
            )
        receipts, spent, failures = [], 0.0, []
        steps = list(plan["steps"])
        fallbacks = list(plan.get("fallback_steps", []))
        adaptive_replanned = False
        for step in steps:
            checkpoint_row = self.conn.execute(
                "SELECT status,attempt,cursor_json,cost,receipt_json FROM source_plan_checkpoints WHERE run_id=? AND step_id=?",
                [run_id, step["step_id"]],
            ).fetchone()
            if checkpoint_row and checkpoint_row[0] == "completed":
                receipt = _load(checkpoint_row[4], {})
                receipts.append(receipt)
                spent += float(checkpoint_row[3])
                continue
            if cancelled and cancelled():
                status = "cancelled"
                break
            current_row = self.conn.execute(
                "SELECT capability_id FROM source_capability_current WHERE namespace=? AND source_id=?",
                [namespace, step["source_id"]],
            ).fetchone()
            if not current_row or current_row[0] != step["capability_id"]:
                failure = {
                    "source_id": step["source_id"],
                    "code": "stale-capability",
                    "retryable": False,
                }
                failures.append(failure)
                receipts.append(
                    {
                        "step_id": step["step_id"],
                        "status": "failed",
                        "failure": failure,
                        "cost": 0,
                    }
                )
                if fallbacks:
                    steps.append(fallbacks.pop(0))
                    adaptive_replanned = True
                continue
            capability = self.capability(
                namespace, step["capability_id"], scopes={READ_SCOPE}
            )
            checkpoint = None if not checkpoint_row else _load(checkpoint_row[2], {})
            max_attempts = int(plan["constraints"].get("retries", 1)) + 1
            output: Mapping[str, Any] = {}
            attempt = int(checkpoint_row[1]) if checkpoint_row else 0
            while attempt < max_attempts:
                attempt += 1
                try:
                    output = dict(runner(capability, step, checkpoint))
                    _reject_secrets(output)
                except Exception as exc:  # noqa: BLE001 - connector isolation boundary
                    output = {
                        "status": "failed",
                        "error": {
                            "code": getattr(exc, "code", "source-failed"),
                            "message": str(exc)[:240],
                        },
                    }
                code = str((output.get("error") or {}).get("code", ""))
                if output.get("status") in {"completed", "complete"} or code not in {
                    "rate_limited",
                    "source_unavailable",
                    "source_timeout",
                }:
                    break
            step_cost = max(
                0.0,
                float(
                    output.get(
                        "cost",
                        step["projected_cost"]
                        if output.get("status") in {"completed", "complete"}
                        else 0,
                    )
                ),
            )
            if spent + step_cost > float(plan["budget"]["limit"]):
                output = {
                    "status": "failed",
                    "error": {
                        "code": "budget-exhausted",
                        "message": "actual cost exceeds plan budget",
                    },
                }
                step_cost = 0
            succeeded = output.get("status") in {"completed", "complete"}
            receipt = {
                "step_id": step["step_id"],
                "source_id": step["source_id"],
                "capability_id": step["capability_id"],
                "status": "completed" if succeeded else "failed",
                "attempts": attempt,
                "cursor": output.get("cursor"),
                "counts": output.get("counts", {}),
                "cost": step_cost,
                "output_hash": _digest(output),
                "failure": None
                if succeeded
                else output.get("error", {"code": "source-failed"}),
            }
            if not succeeded:
                failures.append({"source_id": step["source_id"], **receipt["failure"]})
                if fallbacks:
                    steps.append(fallbacks.pop(0))
                    adaptive_replanned = True
            spent += step_cost
            receipts.append(receipt)
            updated = self.now()
            self.conn.execute(
                "INSERT OR REPLACE INTO source_plan_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    run_id,
                    step["step_id"],
                    step["source_id"],
                    step["capability_id"],
                    receipt["status"],
                    attempt,
                    _canonical(output.get("cursor") or {}),
                    step_cost,
                    _canonical(receipt),
                    updated,
                ],
            )
            self.conn.execute(
                "UPDATE source_plan_runs SET budget_spent=?,updated_at_ms=? WHERE run_id=?",
                [spent, updated, run_id],
            )
        else:
            status = (
                "completed"
                if not failures
                else "partial"
                if any(item["status"] == "completed" for item in receipts)
                else "failed"
            )
        receipt = {
            "contract": RECEIPT_CONTRACT,
            "run_id": run_id,
            "namespace": namespace,
            "plan_id": plan_id,
            "status": status,
            "steps": receipts,
            "failures": failures,
            "budget": {"limit": plan["budget"]["limit"], "spent": round(spent, 8)},
            "checkpointed": True,
            "adaptive_replanned": adaptive_replanned,
            "adaptive_replan_required": bool(failures) and not adaptive_replanned,
        }
        receipt_hash = _digest(receipt)
        receipt["receipt_hash"] = receipt_hash
        finished = self.now()
        self.conn.execute(
            "UPDATE source_plan_runs SET status=?,budget_spent=?,receipt_json=?,updated_at_ms=? WHERE run_id=?",
            [status, spent, _canonical(receipt), finished, run_id],
        )
        self._audit(
            namespace, "execute", run_id, principal_id, {"status": status}, finished
        )
        return {**receipt, "idempotent": False}

    def inspect_run(
        self, namespace: str, run_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT status,receipt_json,budget_spent,created_at_ms,updated_at_ms FROM source_plan_runs WHERE namespace=? AND run_id=?",
            [namespace, run_id],
        ).fetchone()
        if not row:
            return None
        receipt = _load(row[1], {})
        return receipt or {
            "run_id": run_id,
            "namespace": namespace,
            "status": row[0],
            "budget_spent": float(row[2]),
            "created_at_ms": int(row[3]),
            "updated_at_ms": int(row[4]),
        }

    def replay(
        self, namespace: str, run_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        receipt = self.inspect_run(namespace, run_id, scopes=scopes)
        if not receipt or not receipt.get("receipt_hash"):
            raise SourcePlannerError(
                "receipt_not_found", "completed source-plan receipt does not exist"
            )
        base = {
            k: v for k, v in receipt.items() if k not in {"receipt_hash", "idempotent"}
        }
        replayed = _digest(base)
        return {
            "run_id": run_id,
            "stored_hash": receipt["receipt_hash"],
            "replayed_hash": replayed,
            "deterministic": replayed == receipt["receipt_hash"],
        }


def scholarly_decomposition(question, *, author=None, from_date=None, to_date=None):
    """Build provider-neutral scholarly parts for create_objective's existing receipt."""
    from datetime import date
    if not isinstance(question,str) or not question.strip():
        raise ValueError('scholarly question required')
    parameters={'query':question.strip()}
    if author:parameters['author']=author
    for key,value in [('from_date',from_date),('to_date',to_date)]:
        if value is not None:parameters[key]=date.fromisoformat(value).isoformat()
    if from_date and to_date and from_date>to_date:raise ValueError('inverted scholarly date window')
    return [{'question':question.strip(),'query_form':'search','parameters':parameters}]
