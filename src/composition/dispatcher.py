"""Authorized workflow dispatch through registered bindings (C07.3-C07.5).

The dispatcher runs a workflow template for one consumer pack under a stored
plan. It compiles the template to a research recipe and executes it through the
existing recipe runner, supplying one adapter per step. Each adapter:

* invokes only the registered binding the plan names for that consumer and
  capability; unbound or unregistered targets are rejected before any call;
* refuses an effect class the step did not declare (bindings also call
  :meth:`DispatchContext.require_effect` before mutating);
* rechecks authority on every step - a successful preflight is an
  observation, never durable permission - and evaluates the binding's gate
  whichever pack reached it, so a shared capability cannot bypass an
  OSINT-specific gate;
* validates arguments against the binding's schema and the result against its
  declared contract;
* records a step receipt keyed by an idempotency key. Operations whose owner
  supports idempotency or a durable execution receipt may be retried or
  adopted from the owner's receipt; anything else that may have taken effect
  stops with an unknown outcome instead of a blind retry. There is no generic
  exactly-once promise.

Only this module can start ``composition-dispatch`` recipe runs (it holds the
token the runner checks), and such runs report ``actions_executed`` only when a
bound operation was actually invoked and its result validated.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from src.composition.contracts import SCHEMA_DIR, CompositionError, canonical, digest
from src.composition.workflows import WorkflowStore, compile_recipe
from src.kb.research_recipes import (
    EXECUTE_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    RecipeError,
    ResearchRecipeStore,
)

_TOKEN = object()
_CLASSIFICATION = ("public", "internal", "restricted")


def valid_token(token: Any) -> bool:
    return token is _TOKEN


@dataclass
class DispatchContext:
    """What a registered binding may use while it runs one step."""

    conn: Any
    namespace: str
    principal_id: str
    scopes: set[str]
    run_id: str
    step_id: str
    idempotency_key: str
    declared_effect: str
    network: str = "disabled"
    consumer: str = ""
    session_id: str | None = None
    project_id: str | None = None
    inputs: Mapping[str, Any] = field(default_factory=dict)

    def require_effect(self, effect: str) -> None:
        """Raise unless the step declared ``effect`` (read-only is always allowed)."""

        if effect != "read-only" and effect != self.declared_effect:
            raise RecipeError(
                "effect_violation",
                f"step {self.step_id} declared {self.declared_effect} but attempted {effect}",
                step_id=self.step_id, attempted=effect, declared=self.declared_effect,
            )


def _has(scopes: Iterable[str], scope: str) -> bool:
    scopes = set(scopes)
    return "operator" in scopes or scope in scopes


def _resolve_arguments(value: Any, state: Mapping[str, Any]) -> Any:
    """Substitute ``{"$param": name}`` and ``{"$step": "id.path"}`` references."""

    if isinstance(value, Mapping):
        if set(value) == {"$param"}:
            return state["inputs"].get(value["$param"])
        if set(value) == {"$step"}:
            step_id, *path = str(value["$step"]).split(".")
            return _pluck(state["steps"].get(step_id), path)
        return {k: _resolve_arguments(v, state) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_arguments(item, state) for item in value]
    return value


def _pluck(current: Any, path: list[str]) -> Any:
    """Follow ``a.b[].c`` paths; ``[]`` maps the rest of the path over a list."""

    for index, part in enumerate(path):
        if current is None:
            return None
        if part.endswith("[]"):
            items = current.get(part[:-2]) if isinstance(current, Mapping) else None
            return [_pluck(item, path[index + 1:]) for item in (items or [])]
        current = current.get(part) if isinstance(current, Mapping) else None
    return current


def _validate_json(payload: Any, schema: Mapping[str, Any], code: str, what: str) -> None:
    from jsonschema import Draft7Validator

    errors = sorted(Draft7Validator(schema).iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        raise RecipeError(code, f"{what} is invalid: {errors[0].message}")


def _result_schema(name: str) -> Mapping[str, Any] | None:
    path = SCHEMA_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class Dispatcher:
    """Runs workflow templates for a consumer under a stored composition plan."""

    def __init__(
        self,
        conn: Any,
        *,
        authority: Callable[[str], Iterable[str]],
        coordinator: Any = None,
        plan: Mapping[str, Any] | None = None,
        providers: Iterable[Mapping[str, Any]] | None = None,
        now: Callable[[], int] | None = None,
        inputs: Mapping[str, Any] | None = None,
    ) -> None:
        self.conn = conn
        self.authority = authority
        self.coordinator = coordinator
        self._plan = plan
        self._providers = None if providers is None else list(providers)
        self.now = now or (lambda: int(time.time() * 1000))
        self.inputs = dict(inputs or {})
        self.workflows = WorkflowStore(conn, now=self.now)
        self.recipes = ResearchRecipeStore(conn, now=self.now)

    # -- plan and providers ---------------------------------------------------
    def _active_plan(self) -> dict[str, Any]:
        if self._plan is not None:
            return dict(self._plan)
        if self.coordinator is None:
            raise CompositionError("no_plan", "dispatch needs a plan or a coordinator")
        plan = self.coordinator.store.active_plan()
        if plan is None:
            raise CompositionError("no_active_generation", "no composition generation is active")
        return plan

    def _plan_by_digest(self, plan_digest: str) -> dict[str, Any]:
        if self._plan is not None and self._plan["digest"] == plan_digest:
            return dict(self._plan)
        plan = None if self.coordinator is None else self.coordinator.store.plan(plan_digest)
        if plan is None:
            raise CompositionError("unknown_plan", f"plan {plan_digest} is not stored")
        return plan

    def providers(self) -> list[dict[str, Any]]:
        if self._providers is not None:
            return list(self._providers)
        return self.coordinator.store.providers() if self.coordinator else []

    def _scopes(self, principal_id: str) -> set[str]:
        return set(self.authority(principal_id))

    # -- preparation ----------------------------------------------------------
    def _steps(self, template: Mapping[str, Any], plan: Mapping[str, Any], consumer: str):
        """Per step: (binding, registered binding); rejects unbound targets before any call."""

        from src.composition import bindings as registry

        by_capability = {(b["consumer"], b["capability"]): b for b in plan["bindings"]}
        prepared = {}
        for step in template["steps"]:
            binding = by_capability.get((consumer, step["capability"]))
            if binding is None:
                if step.get("optional"):
                    continue
                raise CompositionError(
                    "unbound_step", f"required step {step['id']} ({step['capability']}) is not bound",
                    step_id=step["id"])
            targets = [b["id"] for b in binding.get("bindings", []) if b["kind"] == "registered"]
            registered = registry.binding(targets[0]) if targets else None
            if registered is None:
                raise CompositionError(
                    "unregistered_binding",
                    f"step {step['id']} has no registered binding for {step['capability']}",
                    step_id=step["id"])
            if binding["effect"] not in {"read-only", step["effect"]}:
                raise CompositionError(
                    "effect_violation",
                    f"step {step['id']} declared {step['effect']} but its binding performs {binding['effect']}",
                    step_id=step["id"], declared=step["effect"], attempted=binding["effect"])
            prepared[step["id"]] = (step, binding, registered)
        return prepared

    def _required_scopes(self, binding: Mapping[str, Any]) -> list[str]:
        for descriptor in self.providers():
            if (descriptor["provider_id"], descriptor["version"]) == (
                    binding["provider_id"], binding["provider_version"]):
                for item in descriptor["capabilities"]:
                    if item["capability"] == binding["capability"]:
                        return list(item.get("required_scopes", []))
        return []

    def _preflight(self, template, plan, consumer, principal_id, network) -> dict[str, Any]:
        from src.composition.readiness import assess

        context: dict[str, Any] = {}
        if self.coordinator is not None:
            from src.composition.sources import exhausted_providers

            context["shutdown_providers"] = self.coordinator.shutdown_providers()
            context["exhausted_accounts"] = exhausted_providers(self.conn, self.providers(), now=self.now)
        assessment = assess(plan, self.providers(), conn=self.conn, scopes=self._scopes(principal_id),
                            network=network, visible_consumers=[consumer], observed_at_ms=self.now(),
                            **context)
        required = {s["capability"] for s in template["steps"] if not s.get("optional")}
        # Data a workflow acquires itself cannot exist before it runs: empty data
        # is not a blocker for steps downstream of an acquisition step.
        downstream: set[str] = set()
        acquiring: set[str] = set()
        for step in template["steps"]:
            if step["effect"] == "acquisition" or set(step.get("depends_on", [])) & acquiring:
                acquiring.add(step["id"])
                if step["effect"] != "acquisition":
                    downstream.add(step["capability"])
        blocking = []
        for op in assessment["operations"]:
            if op["capability"] not in required or op["state"] != "blocked":
                continue
            kinds = sorted({b["kind"] for b in op["blockers"]})
            if op["capability"] in downstream and set(kinds) <= {"empty-data"}:
                continue
            blocking.append({"capability": op["capability"], "operation": op["operation"], "kinds": kinds})
        if blocking:
            raise CompositionError(
                "preflight_blocked",
                f"required operation {blocking[0]['operation']} is blocked: {', '.join(blocking[0]['kinds'])}",
                blocking=blocking)
        return assessment

    # -- adapters ---------------------------------------------------------------
    def _adapter(self, *, run_id, step, binding, registered, namespace, principal_id, network,
                 session_id, project_id, consumer=""):
        from src.composition import bindings as registry

        required_scopes = self._required_scopes(binding)

        def adapter(_recipe_step, state):
            scopes = self._scopes(principal_id)
            if not _has(scopes, EXECUTE_SCOPE):
                raise RecipeError("authority_revoked", "execute authority was revoked after preflight")
            missing = [s for s in required_scopes if not _has(scopes, s)]
            if missing:
                raise RecipeError("unauthorized", f"step {step['id']} needs {missing}")
            arguments = _resolve_arguments(step.get("arguments", {}), state)
            if registered.arguments_schema is not None:
                _validate_json(arguments, registered.arguments_schema, "invalid_arguments",
                               f"arguments of step {step['id']}")
            key = digest([run_id, step["id"], arguments])[7:]
            ctx = DispatchContext(conn=self.conn, namespace=namespace, principal_id=principal_id,
                                  scopes=scopes, run_id=run_id, step_id=step["id"], idempotency_key=key,
                                  declared_effect=step["effect"], network=network,
                                  consumer=consumer, session_id=session_id,
                                  project_id=project_id, inputs=self.inputs)
            if registered.gate is not None:
                gate = registry.gate(registered.gate)
                if gate is None or not gate(ctx):
                    raise RecipeError("gate_denied",
                                      f"gate {registered.gate} denies {registered.binding_id} for this caller")
            mutating = binding["effect"] != "read-only"
            retry_safe = binding.get("idempotent") or registered.lookup is not None
            prior = self.workflows.step_receipt(run_id, step["id"])
            if prior and prior["status"] == "completed" and prior["idempotency_key"] == key:
                return {**prior["result"], "dispatched": True}
            if prior and prior["status"] in {"started", "unknown"} and mutating:
                adopted = registered.lookup(ctx, key) if registered.lookup else None
                if adopted is not None:
                    return self._complete(run_id, step, binding, registered, key, dict(adopted))
                if not retry_safe:
                    self.workflows.record_step(run_id, step["id"], key=key, status="unknown",
                                               effect=binding["effect"], binding_id=registered.binding_id)
                    raise RecipeError("unknown_outcome",
                                      f"step {step['id']} may have taken effect; reconcile before retrying")
            if mutating:
                self.workflows.record_step(run_id, step["id"], key=key, status="started",
                                           effect=binding["effect"], binding_id=registered.binding_id)
            try:
                result = registered.fn(ctx, arguments)
            except RecipeError:
                raise
            except Exception as exc:
                if mutating and not retry_safe:
                    self.workflows.record_step(run_id, step["id"], key=key, status="unknown",
                                               effect=binding["effect"], binding_id=registered.binding_id)
                    raise RecipeError("unknown_outcome",
                                      f"step {step['id']} failed after it may have taken effect: {exc}") from exc
                raise
            return self._complete(run_id, step, binding, registered, key, dict(result))

        return adapter

    def _complete(self, run_id, step, binding, registered, key, result):
        if registered.result_contract is not None:
            document = result.get(registered.result_path) if registered.result_path else result
            if not isinstance(document, Mapping) or document.get("contract") != registered.result_contract:
                raise RecipeError("result_contract_mismatch",
                                  f"step {step['id']} did not return {registered.result_contract}")
            schema = _result_schema(registered.result_contract)
            if schema is not None:
                _validate_json(document, schema, "invalid_result", f"result of step {step['id']}")
        references = list(result.get("references", []))
        self.workflows.record_step(run_id, step["id"], key=key, status="completed",
                                   effect=binding["effect"], binding_id=registered.binding_id,
                                   result=result, references=references)
        return {**result, "dispatched": True}

    # -- public API -------------------------------------------------------------
    def start(
        self,
        template: Mapping[str, Any],
        *,
        namespace: str,
        consumer: str,
        parameters: Mapping[str, Any],
        run_key: str,
        principal_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
        network: str = "disabled",
    ) -> dict[str, Any]:
        """Preflight, bind and run a template for ``consumer`` under the active plan."""

        scopes = self._scopes(principal_id)
        if not _has(scopes, EXECUTE_SCOPE):
            raise CompositionError("unauthorized", "recipe execute authority is required")
        plan = self._active_plan()
        if consumer not in {b["consumer"] for b in plan["bindings"]} | {
                f"{m['name']}@{m['version']}" for m in plan["manifests"]}:
            raise CompositionError("unknown_consumer", f"{consumer} is not part of plan {plan['digest']}")
        self._steps(template, plan, consumer)
        preflight = self._preflight(template, plan, consumer, principal_id, network)
        recipe = compile_recipe(template, namespace=namespace, plan=plan, consumer=consumer)
        registered = self.recipes.register(recipe, principal_id=principal_id, scopes={WRITE_SCOPE})
        run_id = "dispatch:" + digest([namespace, consumer, registered["recipe_revision_id"], run_key])[7:31]
        self.workflows.bind_run(
            run_id, namespace=namespace, recipe_revision_id=registered["recipe_revision_id"],
            template=template, consumer=consumer, plan=plan, preflight=preflight,
            principal_id=principal_id, run_key=run_key, parameters=parameters, network=network,
            session_id=session_id, project_id=project_id)
        if self.coordinator is not None:
            self.coordinator.store.pin_run(run_id, plan["digest"], owner=principal_id)
        return self._execute(run_id)

    def _execute(self, run_id: str) -> dict[str, Any]:
        binding_row = self.workflows.run_binding(run_id)
        plan = self._plan_by_digest(binding_row["plan_digest"])
        template = binding_row["template"]
        prepared = self._steps(template, plan, binding_row["consumer"])
        principal_id = binding_row["principal_id"]
        adapters = {
            step_id: self._adapter(
                run_id=run_id, step=step, binding=binding, registered=registered,
                namespace=binding_row["namespace"], principal_id=principal_id,
                network=binding_row["network"], session_id=binding_row["session_id"],
                project_id=binding_row["project_id"], consumer=binding_row["consumer"])
            for step_id, (step, binding, registered) in prepared.items()
        }
        try:
            receipt = self.recipes.run(
                binding_row["namespace"], binding_row["recipe_revision_id"], binding_row["parameters"],
                run_key=binding_row["run_key"], adapters=adapters, principal_id=principal_id,
                scopes={EXECUTE_SCOPE}, network_allowed=binding_row["network"] == "live",
                execution_input_hash=plan["digest"][7:], execution_mode="composition-dispatch",
                actions_executed=None, dispatch_token=_TOKEN,
            )
        except RecipeError as error:
            status = {"reconciliation_required": "reconciliation_required",
                      "cancelled": "cancelled"}.get(error.code, "failed")
            self.workflows.set_run_status(run_id, status)
            raise
        self.workflows.set_recipe_run(run_id, receipt["run_id"])
        self.workflows.set_run_status(run_id, "completed")
        if self.coordinator is not None:
            self.coordinator.store.release_run(run_id, "finished")
        return {**receipt, "dispatch_run_id": run_id, "plan_digest": plan["digest"],
                "step_receipts": self.workflows.step_receipts(run_id),
                "output_restrictions": self._restrictions(run_id)}

    def resume(self, run_id: str, *, principal_id: str) -> dict[str, Any]:
        """Resume under the original plan; never rebinds to a changed provider."""

        from src.composition.resolver import check_resume

        binding_row = self.workflows.run_binding(run_id)
        if binding_row is None:
            raise CompositionError("unknown_run", "run is not bound to a plan")
        scopes = self._scopes(principal_id)
        if principal_id != binding_row["principal_id"] and "operator" not in scopes:
            raise CompositionError("unauthorized", "only the run owner may resume it")
        if not _has(scopes, EXECUTE_SCOPE):
            raise CompositionError("authority_revoked", "execute authority was revoked")
        plan = self._plan_by_digest(binding_row["plan_digest"])
        if self.coordinator is not None:
            from src.composition.sources import source_pins

            verdict = check_resume(
                plan, manifests=self.coordinator.store.manifests(),
                providers=self.coordinator.store.providers(), contracts=self.coordinator.contracts(),
                source_packs=source_pins(self.conn) or None)
            if verdict["status"] != "current":
                return {**verdict, "dispatch_run_id": run_id, "resumed": False}
        return self._execute(run_id)

    def _visible_reference(self, reference: Mapping[str, Any], namespace: str, scopes: set[str]) -> bool:
        if "operator" in scopes:
            return True
        ref_ns = reference.get("namespace", namespace)
        if ref_ns != namespace and f"namespace:{ref_ns}:read" not in scopes:
            return False
        if reference.get("record_kind") == "document":
            return f"document:{reference.get('record_id')}:read" in scopes or f"namespace:{ref_ns}:read" in scopes
        return True

    def _restrictions(self, run_id: str) -> dict[str, Any]:
        """Aggregate output restrictions from the evidence the run actually referenced."""

        classification = "public"
        redistribution = True
        for row in self.conn.execute(
                "SELECT references_json FROM composition_step_receipts WHERE run_id=?", [run_id]).fetchall():
            for reference in json.loads(row[0]):
                if "classification" not in reference:
                    continue  # provenance references (runs, receipts) carry no terms
                level = reference["classification"]
                if level in _CLASSIFICATION and _CLASSIFICATION.index(level) > _CLASSIFICATION.index(classification):
                    classification = level
                if reference.get("redistribution") is False:
                    redistribution = False
        return {"classification": classification, "redistribution": redistribution}

    def read(self, run_id: str, *, principal_id: str) -> dict[str, Any]:
        """Run status with authority rechecked and inaccessible references redacted."""

        binding_row = self.workflows.run_binding(run_id)
        if binding_row is None:
            raise CompositionError("unknown_run", "run is not bound to a plan")
        scopes = self._scopes(principal_id)
        if not _has(scopes, READ_SCOPE) or (
                principal_id != binding_row["principal_id"] and "operator" not in scopes):
            raise CompositionError("unauthorized", "current read access to this run is required")
        steps = []
        for row in self.conn.execute(
                "SELECT step_id, status, references_json FROM composition_step_receipts WHERE run_id=? "
                "ORDER BY step_id", [run_id]).fetchall():
            references = [r if self._visible_reference(r, binding_row["namespace"], scopes)
                          else {"redacted": True} for r in json.loads(row[2])]
            steps.append({"step_id": row[0], "status": row[1], "references": references})
        return {"dispatch_run_id": run_id, "status": binding_row["status"],
                "plan_digest": binding_row["plan_digest"], "recipe_run_id": binding_row["recipe_run_id"],
                "steps": steps, "output_restrictions": self._restrictions(run_id)}

    def export(self, run_id: str, *, principal_id: str) -> dict[str, Any]:
        """Export the recipe run, rechecking authority and access to each reference."""

        view = self.read(run_id, principal_id=principal_id)
        binding_row = self.workflows.run_binding(run_id)
        if binding_row["recipe_run_id"] is None:
            raise CompositionError("not_completed", "only completed runs can be exported")
        exported = self.recipes.export(binding_row["namespace"], binding_row["recipe_run_id"], scopes={READ_SCOPE})
        redacted = any(r.get("redacted") for s in view["steps"] for r in s["references"])
        if redacted:
            exported = {k: v for k, v in exported.items() if k not in {"outputs", "state", "receipt"}}
            exported["redacted"] = True
        return {**exported, "references": [s["references"] for s in view["steps"]],
                "output_restrictions": view["output_restrictions"]}

    def cancel(self, run_id: str, *, principal_id: str) -> dict[str, Any]:
        """Request cancellation; returns the step-receipt state at cancel time."""

        binding_row = self.workflows.run_binding(run_id)
        recipe_run = self.conn.execute(
            "SELECT run_id FROM research_recipe_runs WHERE namespace=? AND recipe_revision_id=? AND run_key=?",
            [binding_row["namespace"], binding_row["recipe_revision_id"], binding_row["run_key"]]).fetchone()
        if recipe_run is not None:
            self.recipes.cancel(binding_row["namespace"], recipe_run[0], principal_id=principal_id,
                                scopes={EXECUTE_SCOPE})
        snapshot = self.workflows.step_receipts(run_id)
        self.conn.execute(
            "UPDATE composition_run_bindings SET status='cancel-requested', preflight_json=? WHERE run_id=?",
            [canonical({**binding_row["preflight"], "receipts_at_cancel": snapshot}), run_id])
        return {"dispatch_run_id": run_id, "status": "cancel-requested", "step_receipts": snapshot}


__all__ = ["DispatchContext", "Dispatcher", "valid_token"]
