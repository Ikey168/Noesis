"""Versioned declarative research recipes and a checkpointed local runner."""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any

RECIPE_CONTRACT = "noesis-research-recipe-v1"
PREVIEW_CONTRACT = "noesis-research-recipe-preview-v1"
RUN_CONTRACT = "noesis-research-recipe-run-v1"
RECEIPT_CONTRACT = "noesis-research-recipe-receipt-v1"
EXPORT_CONTRACT = "noesis-research-recipe-export-v1"
READ_SCOPE = "knowledge:recipes:read"
WRITE_SCOPE = "knowledge:recipes:write"
EXECUTE_SCOPE = "knowledge:recipes:execute"
DEFAULT_LIMITS = {
    "max_steps": 100,
    "max_concurrency": 4,
    "timeout_ms": 300_000,
    "max_output_bytes": 5_000_000,
    "retries": 1,
}

_DDL = """
CREATE TABLE IF NOT EXISTS research_recipe_revisions(
 recipe_revision_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,recipe_id TEXT NOT NULL,
 version TEXT NOT NULL,recipe_hash TEXT NOT NULL,payload_json TEXT NOT NULL,
 principal_id TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,recipe_id,version));
CREATE TABLE IF NOT EXISTS research_recipe_runs(
 run_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,recipe_revision_id TEXT NOT NULL,
 run_key TEXT NOT NULL,input_hash TEXT NOT NULL,status TEXT NOT NULL,cancel_requested BOOLEAN NOT NULL,
 state_json TEXT NOT NULL,error_json TEXT,receipt_json TEXT,principal_id TEXT NOT NULL,
 started_at_ms BIGINT NOT NULL,updated_at_ms BIGINT NOT NULL,
 UNIQUE(namespace,recipe_revision_id,run_key,input_hash));
CREATE TABLE IF NOT EXISTS research_recipe_checkpoints(
 checkpoint_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,step_id TEXT NOT NULL,ordinal BIGINT NOT NULL,
 status TEXT NOT NULL,attempt BIGINT NOT NULL,input_hash TEXT NOT NULL,output_hash TEXT,
 output_json TEXT,error_json TEXT,tool_version TEXT NOT NULL,started_at_ms BIGINT NOT NULL,
 completed_at_ms BIGINT,UNIQUE(run_id,step_id));
CREATE TABLE IF NOT EXISTS research_recipe_audit(
 audit_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,operation TEXT NOT NULL,object_id TEXT NOT NULL,
 principal_id TEXT NOT NULL,detail_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
"""


class RecipeError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


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
        raise RecipeError("unauthorized", f"missing required scope {required}")


def _bounded(value: int, maximum: int) -> int:
    return min(max(int(value), 1), maximum)


def _redact(value: Any, secrets: Sequence[str]) -> Any:
    text = _canonical(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return json.loads(text)


def validate_recipe(
    recipe: Mapping[str, Any], *, known_tools: set[str] | None = None
) -> dict[str, Any]:
    value = json.loads(json.dumps(recipe))
    required = {
        "recipe_id",
        "version",
        "namespace",
        "inputs",
        "steps",
        "outputs",
        "compatibility",
    }
    missing = required - set(value)
    if missing:
        raise RecipeError("invalid_recipe", f"missing recipe fields: {sorted(missing)}")
    if not all(str(value[k]).strip() for k in ("recipe_id", "version", "namespace")):
        raise RecipeError(
            "invalid_recipe", "recipe identity, version, and namespace are required"
        )
    if not isinstance(value["inputs"], Mapping):
        raise RecipeError("invalid_inputs", "inputs must be a typed object")
    for name, schema in value["inputs"].items():
        if not isinstance(schema, Mapping) or not schema.get("type"):
            raise RecipeError("invalid_inputs", f"input {name} requires a type")
        if schema.get("secret") and "default" in schema:
            raise RecipeError(
                "secret_default", "secret inputs cannot have shareable defaults"
            )
    steps = list(value["steps"])
    if not steps or len(steps) > 100:
        raise RecipeError("invalid_graph", "recipe must have between 1 and 100 steps")
    ids = [str(v.get("id") or "") for v in steps]
    if any(not v for v in ids) or len(set(ids)) != len(ids):
        raise RecipeError("invalid_graph", "step IDs must be unique and non-empty")
    by_id = {v["id"]: v for v in steps}
    incoming = {v: 0 for v in ids}
    children = {v: [] for v in ids}
    for step in steps:
        tool = str(step.get("tool") or "")
        if not tool or known_tools is not None and tool not in known_tools:
            raise RecipeError("unknown_tool", f"unknown tool {tool!r}")
        if not step.get("input_schema") or not step.get("output_schema"):
            raise RecipeError(
                "incompatible_contract",
                f"step {step['id']} needs input and output schemas",
            )
        for dependency in step.get("depends_on", []):
            if dependency not in by_id:
                raise RecipeError("invalid_graph", f"unknown dependency {dependency!r}")
            incoming[step["id"]] += 1
            children[dependency].append(step["id"])
    queue = deque(sorted(k for k, v in incoming.items() if v == 0))
    ordered = []
    while queue:
        current = queue.popleft()
        ordered.append(current)
        for child in sorted(children[current]):
            incoming[child] -= 1
            if incoming[child] == 0:
                queue.append(child)
    if len(ordered) != len(ids):
        raise RecipeError("cycle", "recipe graph contains a cycle")
    limits = {**DEFAULT_LIMITS, **dict(value.get("limits") or {})}
    limits["max_steps"] = _bounded(limits["max_steps"], 100)
    limits["max_concurrency"] = _bounded(limits["max_concurrency"], 8)
    limits["timeout_ms"] = _bounded(limits["timeout_ms"], 3_600_000)
    limits["max_output_bytes"] = _bounded(limits["max_output_bytes"], 50_000_000)
    limits["retries"] = min(max(int(limits["retries"]), 0), 3)
    value.update(
        {
            "contract": RECIPE_CONTRACT,
            "steps": [by_id[v] for v in ordered],
            "limits": limits,
            "generation": int(value.get("generation", 0)),
            "valid_time": dict(value.get("valid_time") or {}),
            "observed_at_ms": value.get("observed_at_ms"),
            "producer": dict(value.get("producer") or {}),
            "policy": dict(value.get("policy") or {}),
            "provenance": dict(value.get("provenance") or {}),
        }
    )
    value["recipe_hash"] = _digest(value)
    value["recipe_revision_id"] = "research-recipe:" + value["recipe_hash"][:24]
    return value


class ResearchRecipeStore:
    def __init__(self, conn: Any, *, initialize=True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        conn.execute(_DDL) if initialize else None

    def _audit(self, n, o, i, p, d, t):
        self.conn.execute(
            "INSERT OR IGNORE INTO research_recipe_audit VALUES (?,?,?,?,?,?,?)",
            [
                "recipe-audit:" + _digest([n, o, i, p, d, t])[:24],
                n,
                o,
                i,
                p,
                _canonical(d),
                t,
            ],
        )

    def register(self, recipe, *, principal_id, scopes, known_tools=None):
        _require(scopes, WRITE_SCOPE)
        v = validate_recipe(recipe, known_tools=known_tools)
        row = self.conn.execute(
            "SELECT recipe_hash,payload_json FROM research_recipe_revisions WHERE namespace=? AND recipe_id=? AND version=?",
            [v["namespace"], v["recipe_id"], v["version"]],
        ).fetchone()
        if row:
            if row[0] != v["recipe_hash"]:
                raise RecipeError(
                    "version_conflict", "recipe version has different content"
                )
            return {**_load(row[1], {}), "idempotent": True}
        now = self.now()
        self.conn.execute(
            "INSERT INTO research_recipe_revisions VALUES (?,?,?,?,?,?,?,?)",
            [
                v["recipe_revision_id"],
                v["namespace"],
                v["recipe_id"],
                v["version"],
                v["recipe_hash"],
                _canonical(v),
                principal_id,
                now,
            ],
        )
        self._audit(
            v["namespace"], "register", v["recipe_revision_id"], principal_id, {}, now
        )
        return {**v, "idempotent": False}

    def recipe(self, namespace, recipe_revision_id, *, scopes):
        _require(scopes, READ_SCOPE)
        r = self.conn.execute(
            "SELECT payload_json FROM research_recipe_revisions WHERE namespace=? AND recipe_revision_id=?",
            [namespace, recipe_revision_id],
        ).fetchone()
        if not r:
            raise RecipeError("recipe_not_found", "research recipe not found")
        return _load(r[0], {})

    def list(self, namespace, *, scopes, limit=50, offset=0):
        _require(scopes, READ_SCOPE)
        rows = [
            _load(r[0], {})
            for r in self.conn.execute(
                "SELECT payload_json FROM research_recipe_revisions WHERE namespace=? ORDER BY recipe_id,version",
                [namespace],
            ).fetchall()
        ]
        start = max(int(offset), 0)
        page = rows[start : start + _bounded(limit, 500)]
        return {
            "items": page,
            "total": len(rows),
            "next_offset": start + len(page) if start + len(page) < len(rows) else None,
        }

    def preview(
        self,
        namespace,
        recipe_revision_id,
        parameters,
        *,
        scopes,
        granted_scopes=(),
        allowed_sources=(),
        network_allowed=False,
        available_tool_versions=None,
    ):
        _require(scopes, READ_SCOPE)
        recipe = self.recipe(namespace, recipe_revision_id, scopes=scopes)
        public = {}
        secret_refs = {}
        errors = []
        for name, schema in recipe["inputs"].items():
            supplied = parameters.get(name, schema.get("default"))
            if supplied is None and schema.get("required", False):
                errors.append({"code": "missing_input", "input": name})
                continue
            if schema.get("secret"):
                if not isinstance(supplied, Mapping) or not supplied.get("secret_ref"):
                    errors.append({"code": "unsafe_secret", "input": name})
                else:
                    secret_refs[name] = supplied["secret_ref"]
            else:
                public[name] = supplied
        versions = available_tool_versions or {}
        steps = []
        for step in recipe["steps"]:
            missing = sorted(set(step.get("required_scopes", [])) - set(granted_scopes))
            source = step.get("source_terms")
            denied_source = bool(source and source not in allowed_sources)
            denied_network = bool(step.get("network") and not network_allowed)
            compatible = (
                not step.get("tool_version")
                or versions.get(step["tool"]) == step["tool_version"]
            )
            steps.append(
                {
                    "step_id": step["id"],
                    "tool": step["tool"],
                    "missing_scopes": missing,
                    "source_denied": denied_source,
                    "network_denied": denied_network,
                    "tool_compatible": compatible,
                    "output_classification": step.get(
                        "output_classification", "internal"
                    ),
                }
            )
            if missing or denied_source or denied_network or not compatible:
                errors.append({"code": "policy_gate", "step_id": step["id"]})
        result = {
            "contract": PREVIEW_CONTRACT,
            "namespace": namespace,
            "recipe_revision_id": recipe_revision_id,
            "public_parameters": public,
            "secret_refs": secret_refs,
            "steps": steps,
            "valid": not errors,
            "errors": errors,
        }
        result["preview_hash"] = _digest(result)
        return result

    def run(
        self,
        namespace,
        recipe_revision_id,
        parameters,
        *,
        run_key,
        adapters: Mapping[str, Callable],
        principal_id,
        scopes,
        secret_resolver=None,
        granted_scopes=(),
        allowed_sources=(),
        network_allowed=False,
        tool_versions=None,
        snapshot_tokens=None,
        cancelled=None,
        fail_after=None,
    ):
        _require(scopes, EXECUTE_SCOPE)
        tool_versions = dict(tool_versions or {})
        snapshots = list(snapshot_tokens or [])
        preview = self.preview(
            namespace,
            recipe_revision_id,
            parameters,
            scopes={READ_SCOPE},
            granted_scopes=granted_scopes,
            allowed_sources=allowed_sources,
            network_allowed=network_allowed,
            available_tool_versions=tool_versions,
        )
        if not preview["valid"]:
            raise RecipeError(
                "policy_denied",
                "recipe preview did not pass policy gates",
                errors=preview["errors"],
            )
        if any(v.get("expires_at_ms", self.now() + 1) <= self.now() for v in snapshots):
            raise RecipeError(
                "snapshot_expired", "a pinned research snapshot has expired"
            )
        recipe = self.recipe(namespace, recipe_revision_id, scopes={READ_SCOPE})
        input_hash = _digest(
            [
                preview["public_parameters"],
                preview["secret_refs"],
                snapshots,
                tool_versions,
            ]
        )
        run_id = "recipe-run:" + _digest([recipe_revision_id, run_key, input_hash])[:24]
        now = self.now()
        prior = self.conn.execute(
            "SELECT status,receipt_json FROM research_recipe_runs WHERE run_id=?",
            [run_id],
        ).fetchone()
        if prior and prior[0] == "completed":
            return {**_load(prior[1], {}), "idempotent": True}
        if not prior:
            self.conn.execute(
                "INSERT INTO research_recipe_runs VALUES (?,?,?,?,?,'running',false,?,NULL,NULL,?,?,?)",
                [
                    run_id,
                    namespace,
                    recipe_revision_id,
                    run_key,
                    input_hash,
                    _canonical({}),
                    principal_id,
                    now,
                    now,
                ],
            )
        else:
            self.conn.execute(
                "UPDATE research_recipe_runs SET status='running',error_json=NULL,updated_at_ms=? WHERE run_id=?",
                [now, run_id],
            )
        secret_values = {
            name: (secret_resolver(ref) if secret_resolver else None)
            for name, ref in preview["secret_refs"].items()
        }
        secret_text = [str(v) for v in secret_values.values() if v is not None]
        state = {
            "inputs": {**preview["public_parameters"], **secret_values},
            "steps": {},
        }
        completed = {
            r[0]: _load(r[1], {})
            for r in self.conn.execute(
                "SELECT step_id,output_json FROM research_recipe_checkpoints WHERE run_id=? AND status='completed'",
                [run_id],
            ).fetchall()
        }
        omissions = []
        done = 0
        try:
            for ordinal, step in enumerate(recipe["steps"]):
                if step["id"] in completed:
                    state["steps"][step["id"]] = completed[step["id"]]
                    continue
                cancel_row = self.conn.execute(
                    "SELECT cancel_requested FROM research_recipe_runs WHERE run_id=?",
                    [run_id],
                ).fetchone()
                if cancelled and cancelled() or cancel_row and cancel_row[0]:
                    raise RecipeError("cancelled", "recipe cancelled at a checkpoint")
                adapter = adapters.get(step["tool"])
                if adapter is None:
                    raise RecipeError(
                        "adapter_unavailable", f"no local adapter for {step['tool']}"
                    )
                attempt = 0
                error = None
                output = None
                started = self.now()
                while attempt <= recipe["limits"]["retries"]:
                    attempt += 1
                    try:
                        output = dict(adapter(step, _redact(state, secret_text)))
                        error = None
                        break
                    except Exception as exc:  # noqa: BLE001 - adapter boundary
                        error = {"code": "step_failed", "message": str(exc)[:200]}
                if error:
                    if step.get("optional"):
                        omissions.append({"step_id": step["id"], "error": error})
                        continue
                    raise RecipeError(
                        "step_failed", error["message"], step_id=step["id"]
                    )
                encoded = _canonical(output).encode()
                if len(encoded) > recipe["limits"]["max_output_bytes"]:
                    raise RecipeError(
                        "output_too_large", "step output exceeded recipe budget"
                    )
                clean = _redact(output, secret_text)
                ih = _digest(_redact(state, secret_text))
                oh = _digest(clean)
                cid = "recipe-checkpoint:" + _digest([run_id, step["id"], ih, oh])[:24]
                completed_at = self.now()
                self.conn.execute(
                    "INSERT INTO research_recipe_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,step_id) DO UPDATE SET status=excluded.status,attempt=excluded.attempt,input_hash=excluded.input_hash,output_hash=excluded.output_hash,output_json=excluded.output_json,error_json=NULL,tool_version=excluded.tool_version,completed_at_ms=excluded.completed_at_ms",
                    [
                        cid,
                        run_id,
                        step["id"],
                        ordinal,
                        "completed",
                        attempt,
                        ih,
                        oh,
                        _canonical(clean),
                        None,
                        tool_versions.get(step["tool"], "unknown"),
                        started,
                        completed_at,
                    ],
                )
                state["steps"][step["id"]] = clean
                done += 1
                if fail_after is not None and done >= fail_after:
                    raise RecipeError(
                        "injected_failure", "crash injected after durable checkpoint"
                    )
            safe_state = _redact(state, secret_text)
            receipt = {
                "contract": RECEIPT_CONTRACT,
                "namespace": namespace,
                "run_id": run_id,
                "recipe_revision_id": recipe_revision_id,
                "recipe_hash": recipe["recipe_hash"],
                "status": "completed",
                "public_inputs": preview["public_parameters"],
                "secret_refs": preview["secret_refs"],
                "snapshot_tokens": snapshots,
                "tool_versions": tool_versions,
                "outputs": safe_state["steps"],
                "omissions": omissions,
                "output_hash": _digest(safe_state["steps"]),
                "created_at_ms": self.now(),
            }
            receipt["receipt_hash"] = _digest(receipt)
            self.conn.execute(
                "UPDATE research_recipe_runs SET status='completed',state_json=?,receipt_json=?,updated_at_ms=? WHERE run_id=?",
                [_canonical(safe_state), _canonical(receipt), self.now(), run_id],
            )
            self._audit(
                namespace,
                "run",
                run_id,
                principal_id,
                {"steps": len(safe_state["steps"])},
                self.now(),
            )
            return {**receipt, "idempotent": False}
        except Exception as exc:
            code = getattr(exc, "code", "run_failed")
            status = "cancelled" if code == "cancelled" else "failed"
            error = {"code": code, "message": str(exc)[:300]}
            self.conn.execute(
                "UPDATE research_recipe_runs SET status=?,state_json=?,error_json=?,updated_at_ms=? WHERE run_id=?",
                [
                    status,
                    _canonical(_redact(state, secret_text)),
                    _canonical(error),
                    self.now(),
                    run_id,
                ],
            )
            raise

    def status(self, namespace, run_id, *, scopes):
        _require(scopes, READ_SCOPE)
        r = self.conn.execute(
            "SELECT status,state_json,error_json,receipt_json,started_at_ms,updated_at_ms FROM research_recipe_runs WHERE namespace=? AND run_id=?",
            [namespace, run_id],
        ).fetchone()
        if not r:
            raise RecipeError("run_not_found", "recipe run not found")
        checkpoints = [
            {
                "step_id": v[0],
                "status": v[1],
                "attempt": v[2],
                "input_hash": v[3],
                "output_hash": v[4],
                "tool_version": v[5],
            }
            for v in self.conn.execute(
                "SELECT step_id,status,attempt,input_hash,output_hash,tool_version FROM research_recipe_checkpoints WHERE run_id=? ORDER BY ordinal",
                [run_id],
            ).fetchall()
        ]
        return {
            "contract": RUN_CONTRACT,
            "namespace": namespace,
            "run_id": run_id,
            "status": r[0],
            "state": _load(r[1], {}),
            "error": _load(r[2], None),
            "receipt": _load(r[3], None),
            "checkpoints": checkpoints,
            "started_at_ms": r[4],
            "updated_at_ms": r[5],
        }

    def cancel(self, namespace, run_id, *, principal_id, scopes):
        _require(scopes, EXECUTE_SCOPE)
        r = self.conn.execute(
            "SELECT 1 FROM research_recipe_runs WHERE namespace=? AND run_id=?",
            [namespace, run_id],
        ).fetchone()
        if not r:
            raise RecipeError("run_not_found", "recipe run not found")
        self.conn.execute(
            "UPDATE research_recipe_runs SET cancel_requested=true WHERE run_id=?",
            [run_id],
        )
        return {"run_id": run_id, "cancel_requested": True}

    def replay(self, namespace, run_id, *, scopes, current_tool_versions=None):
        state = self.status(namespace, run_id, scopes=scopes)
        receipt = state["receipt"]
        if not receipt:
            raise RecipeError("receipt_unavailable", "run has no completed receipt")
        output_match = _digest(receipt["outputs"]) == receipt["output_hash"]
        versions_match = (
            current_tool_versions is None
            or dict(current_tool_versions) == receipt["tool_versions"]
        )
        actual = _digest({k: v for k, v in receipt.items() if k != "receipt_hash"})
        return {
            "run_id": run_id,
            "output_match": output_match,
            "tool_versions_match": versions_match,
            "receipt_hash_match": actual == receipt["receipt_hash"],
            "deterministic": output_match
            and versions_match
            and actual == receipt["receipt_hash"],
        }

    def export(self, namespace, run_id, *, scopes):
        state = self.status(namespace, run_id, scopes=scopes)
        recipe = self.recipe(
            namespace, state["receipt"]["recipe_revision_id"], scopes=scopes
        )
        payload = {
            "contract": EXPORT_CONTRACT,
            "namespace": namespace,
            "recipe": recipe,
            "run": state,
            "dependency_complete": True,
        }
        payload["export_hash"] = _digest(payload)
        return payload
