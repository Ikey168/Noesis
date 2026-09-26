"""Workflow templates, plan-bound runs and the composition dispatcher (C07).

* **Templates (C07.1).** A ``noesis-workflow-template-v1`` declares
  parameters, required capabilities with contract ranges, ordered steps with
  declared effects, and expected artifacts. Investigation templates adapt to
  it without losing their source-pack pins.
* **Bindings (C07.2).** Sessions, projects and recipe runs record the
  workflow and the plan digest they execute under in
  ``composition_workflow_bindings``, next to (never inside) their existing
  state. Resuming checks the stored plan and reports ``new_plan_required``
  instead of silently rebinding. A readiness preflight is stored with the
  binding as an observation, never as permission.
* **Dispatcher (C07.3/C07.4).** Steps execute only through the operation a
  plan binding names and a registered handler implements. Declared effect
  classes are enforced before and during the call; authority and owner gates
  are rechecked on execute, resume, read and export. Retries happen only
  where the owner declares idempotency or a durable receipt; a crash after an
  external effect is reconciled from the owner's receipt or the step stops
  with an unknown outcome. Nothing here claims exactly-once execution.
* **Honest execution mode (C07.5).** Only a :class:`DispatchAttestation`
  lets a recipe run report ``actions_executed: true`` in dispatch mode; the
  public fixture runner keeps reporting ``false``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.composition.contracts import (
    SIDE_EFFECTS,
    WORKFLOW_CONTRACT,
    CompositionContractError,
    canonical_json,
    content_hash,
    plan_digest,
    validate_workflow_template,
)
from src.composition.readiness import CompositionView, assess, redact
from src.composition.resolver import RESOLVER_VERSION, resume

SUBJECT_KINDS = ("intake_session", "research_project", "recipe_run", "workflow_run")
DISPATCH_MODE = "composition-dispatch"
_EFFECT_RANK = {effect: rank for rank, effect in enumerate(SIDE_EFFECTS)}

_DDL = """
CREATE TABLE IF NOT EXISTS composition_workflow_bindings(
 subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, namespace TEXT NOT NULL, workflow_id TEXT NOT NULL,
 workflow_version TEXT NOT NULL, workflow_hash TEXT NOT NULL, mode TEXT, profile TEXT, plan_digest TEXT NOT NULL,
 resolver_version TEXT NOT NULL, preflight_json TEXT, bound_at_ms BIGINT NOT NULL,
 PRIMARY KEY(subject_kind, subject_id));
CREATE TABLE IF NOT EXISTS composition_step_receipts(
 run_id TEXT NOT NULL, step_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, capability TEXT NOT NULL,
 operation TEXT NOT NULL, provider TEXT NOT NULL, tool TEXT NOT NULL, effect TEXT NOT NULL, status TEXT NOT NULL,
 attempts BIGINT NOT NULL, result_json TEXT, result_hash TEXT, owner_receipt_json TEXT, detail TEXT,
 started_at_ms BIGINT NOT NULL, completed_at_ms BIGINT, PRIMARY KEY(run_id, step_id));
"""


class DispatchError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class StepOutcomeUnknown(DispatchError):
    """An external effect may have happened and no owner receipt settles it."""


class SimulatedCrash(RuntimeError):
    """Test hook: the process dies after the handler's effect, before the checkpoint."""


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ------------------------------------------------------------ C07.1 templates


def seal_workflow(document: Mapping[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in document.items() if k != "content_hash"}
    return {**body, "content_hash": content_hash(body)}


def require_workflow(document: Mapping[str, Any]) -> dict[str, Any]:
    issues = validate_workflow_template(document)
    if issues:
        raise CompositionContractError(WORKFLOW_CONTRACT, issues)
    return dict(document)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return slug if slug and slug[0].isalpha() else f"t-{slug or 'template'}"


def from_investigation_template(state: Mapping[str, Any], *, requires: Sequence[Mapping[str, Any]] = (),
                                steps: Sequence[Mapping[str, Any]] = (), pack: str | None = None
                                ) -> dict[str, Any]:
    """Adapt a stored investigation template revision to a workflow template.

    Source-pack pins, parameters, questions, success criteria, scope and the
    report outline are carried over unchanged. Investigation templates do not
    name capabilities, so ``requires`` and ``steps`` are supplied by the
    caller (a pack's declaration); the result validates, so a step naming a
    capability the caller did not declare fails.
    """

    definition = state["definition"]
    document = {
        "contract": WORKFLOW_CONTRACT,
        "id": f"investigation.{_slug(str(state['template_id']))}",
        "version": f"{int(state['revision'])}.0.0",
        "description": definition["description"],
        "parameters": {name: {"type": "string", "description": text, "required": True}
                       for name, text in sorted(definition["parameters"].items())},
        "requires": [dict(r) for r in requires],
        "steps": [dict(s) for s in steps],
        "artifacts": [{"id": "report", "kind": "report", "description": definition["name"],
                       "sections": list(definition["report_outline"])}],
        "source_packs": [dict(pin) for pin in definition["source_packs"]],
        "questions": list(definition["questions"]),
        "success_criteria": list(definition["success_criteria"]),
        "scope": dict(definition["scope"]),
        "adapter": {"source": "noesis-investigation-template-v1", "template_id": state["template_id"],
                    "revision": int(state["revision"]), "namespace": state.get("namespace")},
    }
    if pack:
        document["pack"] = pack
    return require_workflow(seal_workflow(document))


# ------------------------------------------------------------ C07.2 bindings


class WorkflowBindings:
    """Plan digests bound to sessions, projects and recipe runs."""

    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        for statement in filter(str.strip, _DDL.split(";")):
            conn.execute(statement)
        conn.execute("CREATE TABLE IF NOT EXISTS composition_plans(digest TEXT PRIMARY KEY, plan_json TEXT NOT NULL, "
                     "selection_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL)")

    def preflight(self, view: CompositionView, workflow: Mapping[str, Any], **options: Any) -> dict[str, Any]:
        """Readiness for exactly the template's operations: an observation, not a grant."""

        document = assess(view, **options)
        wanted = {(s["capability"], s["operation"]) for s in workflow["steps"]}
        document["operations"] = [o for o in document["operations"] if (o["capability"], o["operation"]) in wanted]
        return document

    def bind(self, subject_kind: str, subject_id: str, namespace: str, workflow: Mapping[str, Any],
             plan: Mapping[str, Any], *, mode: str | None = None, profile: str | None = None,
             preflight: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if subject_kind not in SUBJECT_KINDS:
            raise DispatchError("invalid_subject", f"subject kind must be one of {SUBJECT_KINDS}")
        require_workflow(workflow)
        if plan.get("digest") != plan_digest(plan):
            raise DispatchError("invalid_plan", "plan digest does not match the plan")
        if preflight is not None and preflight.get("plan_digest") != plan["digest"]:
            raise DispatchError("invalid_preflight", "preflight assessed a different plan")
        self.conn.execute("INSERT INTO composition_plans VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                          [plan["digest"], _json(plan), "workflow-binding", self.now()])
        existing = self.binding(subject_kind, subject_id)
        if existing and existing["plan_digest"] != plan["digest"]:
            raise DispatchError("already_bound", f"{subject_kind} {subject_id} runs under plan "
                                f"{existing['plan_digest']}; start a new run for a new plan")
        self.conn.execute(
            "INSERT INTO composition_workflow_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [subject_kind, subject_id, namespace, workflow["id"], workflow["version"],
             workflow.get("content_hash") or content_hash(workflow), mode, profile, plan["digest"],
             str(plan.get("resolver_version") or RESOLVER_VERSION),
             None if preflight is None else _json(preflight), self.now()])
        return self.binding(subject_kind, subject_id)

    def binding(self, subject_kind: str, subject_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT namespace, workflow_id, workflow_version, workflow_hash, mode, profile, plan_digest, "
            "resolver_version, preflight_json, bound_at_ms FROM composition_workflow_bindings "
            "WHERE subject_kind=? AND subject_id=?", [subject_kind, subject_id]).fetchone()
        if not row:
            return None
        keys = ("namespace", "workflow_id", "workflow_version", "workflow_hash", "mode", "profile", "plan_digest",
                "resolver_version", "preflight", "bound_at_ms")
        record = dict(zip(keys, row, strict=True))
        record["preflight"] = json.loads(record["preflight"]) if record["preflight"] else None
        return {"subject_kind": subject_kind, "subject_id": subject_id, **record}

    def plan(self, digest: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT plan_json FROM composition_plans WHERE digest=?", [digest]).fetchone()
        return json.loads(row[0]) if row else None

    def resume(self, subject_kind: str, subject_id: str, candidates: Sequence[Mapping[str, Any]],
               providers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Replay rules of C03.4 on the stored digest; never rebinds silently."""

        binding = self.binding(subject_kind, subject_id)
        if binding is None:
            raise DispatchError("not_bound", f"{subject_kind} {subject_id} has no workflow binding")
        plan = self.plan(binding["plan_digest"])
        result = resume(plan, candidates, providers)
        return {"status": result.status, "plan_digest": binding["plan_digest"], "diff": result.diff,
                "unavailable": result.unavailable,
                "proposed_plan_digest": result.replan.plan["digest"] if result.replan and result.replan.ok else None}


# ------------------------------------------------------------ C07.3 / C07.4 dispatcher


def osint_review_gate(principal: str, scopes: Iterable[str]) -> bool:
    """The OSINT review gate: the explicit sign-off switch (docs/security/osint-review-gate.md)."""

    from src.config.env import resolve_env

    return (resolve_env("OSINT_GATED_TOOLS", "off") or "off").lower() in ("on", "1", "true")


DEFAULT_GATES: dict[str, Callable[[str, Iterable[str]], bool]] = {"osint-review": osint_review_gate}


class StepContext:
    """What a handler may do: effects are checked against the step's declaration."""

    def __init__(self, declared: str, idempotency_key: str, cancelled: Callable[[], bool] | None) -> None:
        self.declared = declared
        self.idempotency_key = idempotency_key
        self._cancelled = cancelled
        self.effects: list[str] = []

    def record_effect(self, effect: str) -> None:
        if effect not in _EFFECT_RANK:
            raise DispatchError("unknown_effect", f"unknown effect class {effect!r}")
        if _EFFECT_RANK[effect] > _EFFECT_RANK[self.declared]:
            raise DispatchError("effect_violation", f"step declared {self.declared!r} but attempted {effect!r}",
                                declared=self.declared, attempted=effect)
        self.effects.append(effect)

    def cancelled(self) -> bool:
        return bool(self._cancelled and self._cancelled())


class WorkflowDispatcher:
    """Invoke only registered plan bindings, rechecking authority at every step."""

    def __init__(self, conn: Any, plan: Mapping[str, Any], descriptors: Iterable[Mapping[str, Any]],
                 handlers: Mapping[str, Callable[[Mapping[str, Any], StepContext], Mapping[str, Any]]], *,
                 gates: Mapping[str, Callable[[str, Iterable[str]], bool]] | None = None,
                 receipt_lookup: Mapping[str, Callable[[str], Mapping[str, Any] | None]] | None = None,
                 contract_validators: Mapping[str, Callable[[Any], bool]] | None = None,
                 max_attempts: int = 3, now: Callable[[], int] | None = None) -> None:
        if plan.get("digest") != plan_digest(plan):
            raise DispatchError("invalid_plan", "plan digest does not match the plan")
        self.conn = conn
        self.plan = plan
        pins = {(p["id"], p["version"]) for p in plan["providers"]}
        self.descriptors = {d["id"]: d for d in descriptors if (d["id"], d["version"]) in pins}
        self.handlers = dict(handlers)
        self.gates = {**DEFAULT_GATES, **dict(gates or {})}
        self.receipt_lookup = dict(receipt_lookup or {})
        self.contract_validators = dict(contract_validators or {})
        self.max_attempts = max_attempts
        self.now = now or (lambda: int(time.time() * 1000))
        self.bindings = {b["capability"]: b for b in plan["bindings"]}
        for statement in filter(str.strip, _DDL.split(";")):
            conn.execute(statement)

    # -- resolution and authority

    def _operation(self, step: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        binding = self.bindings.get(step["capability"])
        if binding is None or step["operation"] not in binding["operations"]:
            raise DispatchError("unbound_operation", f"step {step['id']!r}: {step['capability']}:{step['operation']} "
                                "is not bound in the run's plan", step=step["id"])
        descriptor = self.descriptors.get(binding["provider"])
        if descriptor is None:
            raise DispatchError("unregistered_target", f"provider {binding['provider']} is not registered")
        operation = next(op for op in descriptor["operations"] if op["id"] == step["operation"])
        if operation["tool"] not in self.handlers:
            raise DispatchError("unregistered_target", f"no registered handler implements {operation['tool']}",
                                step=step["id"], tool=operation["tool"])
        if operation["side_effect"] != step["effect"]:
            raise DispatchError("effect_mismatch", f"step {step['id']!r} declares {step['effect']!r} but "
                                f"{operation['id']} is {operation['side_effect']!r}", step=step["id"],
                                declared=step["effect"], operation_effect=operation["side_effect"])
        return binding, operation

    def _authorize(self, action: str, step: Mapping[str, Any], operation: Mapping[str, Any], principal: str,
                   scopes: Iterable[str], authorize: Callable[[str, str, Mapping[str, Any]], bool] | None) -> None:
        scopes = set(scopes)
        missing = sorted(set(operation.get("required_scopes") or []) - scopes)
        if missing:
            raise DispatchError("unauthorized", f"{action} {step['id']!r} needs scopes {missing}", action=action)
        if authorize is not None and not authorize(action, principal, step):
            raise DispatchError("unauthorized", f"{action} {step['id']!r} is no longer authorized", action=action)
        gates = list(operation.get("gates") or [])
        from src.osint.investigations import is_gated

        if is_gated(operation["tool"].split(".", 1)[-1]) and "osint-review" not in gates:
            gates.append("osint-review")
        for gate in gates:
            check = self.gates.get(gate)
            if check is None or not check(principal, scopes):
                raise DispatchError("gate_denied", f"{operation['tool']} is behind the {gate!r} gate, which "
                                    "has not passed; a shared capability does not bypass it", gate=gate)

    def _validate(self, contract: str | None, value: Any, what: str) -> None:
        if not isinstance(value, Mapping):
            raise DispatchError(f"invalid_{what}", f"{what} must be an object")
        if contract and contract in self.contract_validators and not self.contract_validators[contract](value):
            raise DispatchError(f"invalid_{what}", f"{what} does not satisfy {contract}", contract=contract)

    # -- step receipts

    def _row(self, run_id: str, step_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT idempotency_key, status, attempts, result_json, result_hash, owner_receipt_json, detail "
            "FROM composition_step_receipts WHERE run_id=? AND step_id=?", [run_id, step_id]).fetchone()
        if not row:
            return None
        return {"idempotency_key": row[0], "status": row[1], "attempts": row[2],
                "result": json.loads(row[3]) if row[3] else None, "result_hash": row[4],
                "owner_receipt": json.loads(row[5]) if row[5] else None, "detail": row[6]}

    def _write(self, run_id: str, step: Mapping[str, Any], binding: Mapping[str, Any], operation: Mapping[str, Any],
               key: str, status: str, attempts: int, *, result: Any = None, owner_receipt: Any = None,
               detail: str | None = None) -> None:
        now = self.now()
        self.conn.execute(
            "INSERT INTO composition_step_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (run_id, step_id) "
            "DO UPDATE SET status=excluded.status, attempts=excluded.attempts, result_json=excluded.result_json, "
            "result_hash=excluded.result_hash, owner_receipt_json=excluded.owner_receipt_json, detail=excluded.detail, "
            "completed_at_ms=excluded.completed_at_ms",
            [run_id, step["id"], key, step["capability"], operation["id"], binding["provider"], operation["tool"],
             step["effect"], status, attempts, None if result is None else _json(result),
             None if result is None else _digest(result), None if owner_receipt is None else _json(owner_receipt),
             detail, now, now if status in {"completed", "failed", "unknown", "cancelled"} else None])

    def _owner_receipt(self, operation: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
        lookup = self.receipt_lookup.get(operation["tool"])
        return lookup(key) if lookup else None

    # -- execution

    def execute(self, run_id: str, step: Mapping[str, Any], arguments: Mapping[str, Any], *, principal: str,
                scopes: Iterable[str], authorize: Callable[[str, str, Mapping[str, Any]], bool] | None = None,
                cancelled: Callable[[], bool] | None = None, action: str = "execute",
                crash_after_effect: bool = False) -> dict[str, Any]:
        binding, operation = self._operation(step)
        self._authorize(action, step, operation, principal, scopes, authorize)
        self._validate(operation.get("input_contract"), arguments, "arguments")
        key = _digest([run_id, step["id"], arguments])
        idempotency = operation.get("idempotency") or {}
        retryable = bool(idempotency.get("supported") or idempotency.get("receipt"))
        prior = self._row(run_id, step["id"])
        if prior and prior["status"] == "completed":
            return {**prior["result"], "_dispatch": {"adopted": True, "idempotency_key": key}}
        if prior and prior["status"] == "unknown":
            raise StepOutcomeUnknown("unknown_outcome", f"step {step['id']!r} has an unknown outcome and needs "
                                     "reconciliation; it is not retried blindly", step=step["id"])
        if prior and prior["status"] in {"started", "cancelled"}:
            owner = self._owner_receipt(operation, key)
            if owner is not None:
                result = dict(owner.get("result") or owner)
                self._write(run_id, step, binding, operation, key, "completed", prior["attempts"], result=result,
                            owner_receipt=owner, detail="reconciled from owner receipt")
                return {**result, "_dispatch": {"adopted": True, "reconciled": True, "idempotency_key": key}}
            if not idempotency.get("supported"):
                self._write(run_id, step, binding, operation, key, "unknown", prior["attempts"],
                            detail="effect may have happened; no owner receipt found")
                raise StepOutcomeUnknown("unknown_outcome", f"step {step['id']!r} may have taken effect and its "
                                         "owner has no receipt; stopping this step", step=step["id"])
        if cancelled and cancelled():
            raise DispatchError("cancelled", f"run cancelled before step {step['id']!r}")
        attempts = prior["attempts"] if prior else 0
        last_error: Exception | None = None
        while attempts < (self.max_attempts if retryable else 1):
            attempts += 1
            self._write(run_id, step, binding, operation, key, "started", attempts)
            context = StepContext(step["effect"], key, cancelled)
            try:
                result = self.handlers[operation["tool"]](dict(arguments), context)
            except DispatchError as exc:
                self._write(run_id, step, binding, operation, key, "failed", attempts, detail=f"{exc.code}: {exc}")
                raise
            except Exception as exc:  # noqa: BLE001 - owner boundary
                last_error = exc
                continue
            if crash_after_effect:
                raise SimulatedCrash(step["id"])
            if context.cancelled():
                owner = self._owner_receipt(operation, key)
                self._write(run_id, step, binding, operation, key, "cancelled", attempts, owner_receipt=owner,
                            detail="cancelled during the step; owner receipt state recorded")
                raise DispatchError("cancelled", f"run cancelled during step {step['id']!r}",
                                    owner_receipt=bool(owner))
            self._validate(operation.get("output_contract"), result, "result")
            clean = dict(result)
            self._write(run_id, step, binding, operation, key, "completed", attempts, result=clean,
                        owner_receipt=clean.get("receipt"))
            return {**clean, "_dispatch": {"adopted": False, "idempotency_key": key, "attempts": attempts}}
        self._write(run_id, step, binding, operation, key, "failed", attempts,
                    detail=f"{type(last_error).__name__}: {str(last_error)[:200]}")
        raise DispatchError("step_failed", f"step {step['id']!r} failed after {attempts} attempt(s)"
                            + ("" if retryable else "; the owner declares no idempotency, so it was not retried"),
                            step=step["id"], attempts=attempts)

    def run_steps(self, run_id: str, workflow: Mapping[str, Any], arguments: Mapping[str, Mapping[str, Any]], *,
                  principal: str, scopes: Iterable[str], authorize: Callable | None = None,
                  cancelled: Callable[[], bool] | None = None, action: str = "execute") -> dict[str, Any]:
        """Run steps in dependency order; an unknown or failed step stops only its dependents."""

        statuses: dict[str, str] = {}
        results: dict[str, Any] = {}
        pending = [dict(s) for s in workflow["steps"]]
        while pending:
            progressed = False
            for step in list(pending):
                deps = step.get("depends_on") or []
                if any(statuses.get(d) is None for d in deps):
                    continue
                pending.remove(step)
                progressed = True
                if any(statuses[d] != "completed" for d in deps):
                    statuses[step["id"]] = "blocked"
                    continue
                try:
                    results[step["id"]] = self.execute(run_id, step, arguments.get(step["id"], {}),
                                                       principal=principal, scopes=scopes, authorize=authorize,
                                                       cancelled=cancelled, action=action)
                    statuses[step["id"]] = "completed"
                except StepOutcomeUnknown:
                    statuses[step["id"]] = "unknown"
                except DispatchError as exc:
                    if exc.code in {"cancelled", "unauthorized", "gate_denied"}:
                        raise
                    statuses[step["id"]] = "omitted" if step.get("optional") else "failed"
            if not progressed:
                break
        return {"run_id": run_id, "plan_digest": self.plan["digest"], "steps": statuses, "results": results}

    def resume(self, run_id: str, workflow: Mapping[str, Any], arguments: Mapping[str, Mapping[str, Any]],
               **options: Any) -> dict[str, Any]:
        return self.run_steps(run_id, workflow, arguments, action="resume", **options)

    # -- read / export

    def _recheck(self, action: str, run_id: str, principal: str, scopes: Iterable[str],
                 authorize: Callable | None) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT step_id, capability, operation, effect FROM composition_step_receipts "
                                 "WHERE run_id=? ORDER BY step_id", [run_id]).fetchall()
        steps = []
        for step_id, capability, operation_id, effect in rows:
            step = {"id": step_id, "capability": capability, "operation": operation_id, "effect": effect}
            _, operation = self._operation(step)
            self._authorize(action, step, operation, principal, scopes, authorize)
            steps.append({**step, **self._row(run_id, step_id)})
        return steps

    def read(self, run_id: str, *, principal: str, scopes: Iterable[str], authorize: Callable | None = None,
             evidence_visible: Callable[[Mapping[str, Any]], bool] | None = None) -> dict[str, Any]:
        """Step receipts after an authority recheck; inaccessible evidence is omitted."""

        steps = self._recheck("read", run_id, principal, scopes, authorize)
        restrictions: set[str] = set()
        for step in steps:
            result = step.get("result") or {}
            evidence = list(result.get("evidence") or [])
            kept = [e for e in evidence if evidence_visible is None or evidence_visible(e)]
            restrictions |= {r for e in kept for r in e.get("restrictions") or []}
            if evidence:
                step["result"] = {**result, "evidence": kept, "evidence_withheld": len(evidence) - len(kept)}
        return {"run_id": run_id, "steps": steps, "output_restrictions": sorted(restrictions)}

    def export(self, run_id: str, *, principal: str, scopes: Iterable[str], authorize: Callable | None = None,
               evidence_exportable: Callable[[Mapping[str, Any]], bool] | None = None) -> dict[str, Any]:
        """Export only if every referenced evidence item may leave; restrictions come from that evidence."""

        steps = self._recheck("export", run_id, principal, scopes, authorize)
        evidence = [e for s in steps for e in ((s.get("result") or {}).get("evidence") or [])]
        blocked = [e for e in evidence if evidence_exportable is not None and not evidence_exportable(e)]
        if blocked:
            raise DispatchError("export_denied", f"{len(blocked)} referenced evidence item(s) may not be exported",
                                withheld=len(blocked))
        return {"run_id": run_id, "plan_digest": self.plan["digest"],
                "output_restrictions": sorted({r for e in evidence for r in e.get("restrictions") or []}),
                "steps": [{k: s[k] for k in ("id", "capability", "operation", "effect", "status", "result_hash")}
                          for s in steps]}

    def attestation(self, run_id: str) -> DispatchAttestation:
        return DispatchAttestation(self, run_id)


class DispatchAttestation:
    """Evidence that recipe steps were invoked through registered bindings with validated results."""

    def __init__(self, dispatcher: WorkflowDispatcher, run_id: str) -> None:
        self.dispatcher = dispatcher
        self.run_id = run_id

    def verify(self, step_ids: Sequence[str]) -> dict[str, Any]:
        steps = []
        for step_id in step_ids:
            row = self.dispatcher._row(self.run_id, step_id)
            if row is None or row["status"] != "completed" or not row["result_hash"]:
                raise DispatchError("not_dispatched", f"step {step_id!r} was not dispatched to completion")
            record = self.dispatcher.conn.execute(
                "SELECT provider, operation, tool FROM composition_step_receipts WHERE run_id=? AND step_id=?",
                [self.run_id, step_id]).fetchone()
            steps.append({"step_id": step_id, "provider": record[0], "operation": record[1], "tool": record[2],
                          "result_hash": row["result_hash"]})
        return {"dispatcher": "noesis-composition-dispatcher-v1", "run_id": self.run_id,
                "plan_digest": self.dispatcher.plan["digest"], "steps": steps}


# ------------------------------------------------------------ C07.5 recipe integration


def recipe_for(workflow: Mapping[str, Any], namespace: str) -> dict[str, Any]:
    """The recipe the existing recipe store runs for a workflow template."""

    return {"recipe_id": workflow["id"], "version": workflow["version"], "namespace": namespace,
            "inputs": {name: {"type": spec["type"]} for name, spec in workflow["parameters"].items()},
            "steps": [{"id": s["id"], "tool": f"{s['capability']}:{s['operation']}",
                       "depends_on": list(s.get("depends_on") or []), "optional": bool(s.get("optional")),
                       "input_schema": {"type": "object"}, "output_schema": {"type": "object"}}
                      for s in workflow["steps"]],
            "outputs": {a["id"]: {"kind": a["kind"]} for a in workflow["artifacts"]},
            "compatibility": {"workflow_contract": WORKFLOW_CONTRACT}}


def run_workflow(dispatcher: WorkflowDispatcher, recipes: Any, bindings: WorkflowBindings, workflow: Mapping[str, Any],
                 *, namespace: str, run_key: str, parameters: Mapping[str, Any],
                 arguments: Mapping[str, Mapping[str, Any]], principal: str, scopes: Iterable[str],
                 recipe_scopes: Iterable[str], authorize: Callable | None = None,
                 preflight: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Dispatch a workflow, then record it as a recipe run bound to its plan.

    Steps run through the dispatcher first. The recipe store then records the
    run; its adapters call the dispatcher again, which adopts the completed
    step receipts, so no effect happens twice. Only then may the receipt say
    ``actions_executed: true``.
    """

    recipe = recipes.register(recipe_for(workflow, namespace), principal_id=principal, scopes=set(recipe_scopes))
    run_id = f"workflow-run:{_digest([namespace, recipe['recipe_revision_id'], run_key])[:24]}"
    bindings.bind("workflow_run", run_id, namespace, workflow, dispatcher.plan, preflight=preflight)
    outcome = dispatcher.run_steps(run_id, workflow, arguments, principal=principal, scopes=scopes,
                                   authorize=authorize)
    required = [s["id"] for s in workflow["steps"] if not s.get("optional")]
    if any(outcome["steps"].get(step_id) != "completed" for step_id in required):
        return {"status": "incomplete", "workflow_run_id": run_id, "steps": outcome["steps"], "recipe_run": None}
    steps_by_id = {s["id"]: s for s in workflow["steps"]}
    adapters = {
        step_id: (lambda step, state, sid=step_id: {k: v for k, v in dispatcher.execute(
            run_id, steps_by_id[sid], arguments.get(sid, {}), principal=principal, scopes=scopes,
            authorize=authorize).items() if k != "_dispatch"})
        for step_id, status in outcome["steps"].items() if status == "completed"
    }
    receipt = recipes.run(namespace, recipe["recipe_revision_id"], dict(parameters), run_key=run_key,
                          adapters=adapters, principal_id=principal, scopes=set(recipe_scopes),
                          execution_mode=DISPATCH_MODE, actions_executed=True,
                          dispatch_attestation=dispatcher.attestation(run_id))
    bindings.bind("recipe_run", receipt["run_id"], namespace, workflow, dispatcher.plan, preflight=preflight)
    return {"status": "completed", "workflow_run_id": run_id, "steps": outcome["steps"], "recipe_run": receipt}


__all__ = [
    "DEFAULT_GATES", "DISPATCH_MODE", "DispatchAttestation", "DispatchError", "SimulatedCrash", "StepContext",
    "StepOutcomeUnknown", "WorkflowBindings", "WorkflowDispatcher", "from_investigation_template", "recipe_for",
    "redact", "require_workflow", "run_workflow", "seal_workflow",
]
