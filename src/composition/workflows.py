"""Workflow templates and their binding to sessions, projects and recipe runs (C07.1, C07.2).

A workflow template (``noesis-workflow-template-v1``) declares parameters,
required capabilities with contract ranges, ordered steps with declared
effects, permitted record kinds and expected artifacts. It names capabilities,
never providers or stores: resolution chooses providers, and templates compile
to research recipes that run through the existing recipe runner with its
parameter validation, snapshots, budgets, checkpoints and cancellation.

Investigation templates (which pin source packs) adapt into workflow templates
without losing their pins. Intake sessions record their mode, profile and
template reference in ``composition_session_bindings`` next to - not inside -
the existing session state, and recipe runs record the plan digest they execute
under in ``composition_run_bindings``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.composition.contracts import (
    EXECUTABLE_KEYS,
    CompositionError,
    canonical,
    check_range,
    digest,
    schema_errors,
)

TEMPLATE_CONTRACT = "noesis-workflow-template-v1"

DDL = """
CREATE TABLE IF NOT EXISTS composition_session_bindings (
  session_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, mode TEXT NOT NULL,
  profile TEXT, template_id TEXT NOT NULL, template_version TEXT NOT NULL,
  template_hash TEXT NOT NULL, plan_digest TEXT NOT NULL, resolver_version TEXT NOT NULL,
  bound_by TEXT NOT NULL, bound_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS composition_run_bindings (
  run_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, recipe_revision_id TEXT NOT NULL,
  template_id TEXT NOT NULL, template_version TEXT NOT NULL, consumer TEXT NOT NULL,
  session_id TEXT, project_id TEXT, plan_digest TEXT NOT NULL, resolver_version TEXT NOT NULL,
  preflight_json TEXT NOT NULL, principal_id TEXT NOT NULL, status TEXT NOT NULL,
  bound_at_ms BIGINT NOT NULL, recipe_run_id TEXT, run_key TEXT NOT NULL,
  parameters_json TEXT NOT NULL, network TEXT NOT NULL, template_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS composition_step_receipts (
  run_id TEXT NOT NULL, step_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  status TEXT NOT NULL, effect TEXT NOT NULL, binding_id TEXT NOT NULL,
  result_json TEXT, references_json TEXT NOT NULL, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(run_id, step_id)
);
"""


def template_hash(template: Mapping[str, Any]) -> str:
    return digest({k: v for k, v in template.items() if k != "template_hash"})


def validate_template(
    template: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any] | None = None,
    known_capabilities: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate a workflow template.

    With ``manifest``, every step capability must be one the owning pack
    requires, so resolution binds it; otherwise it is an undeclared capability.
    Templates may not declare stores or name code.
    """

    if not isinstance(template, Mapping):
        raise CompositionError("invalid_template", "workflow template must be an object")
    value = json.loads(canonical({k: v for k, v in template.items() if k != "template_hash"}))
    for key, where in _keys(value):
        if key in EXECUTABLE_KEYS:
            raise CompositionError("executable_reference", f"templates cannot name code ({where})")
        if key in {"store", "stores", "tables"}:
            raise CompositionError("template_declares_store", f"templates cannot declare stores ({where})")
    errors = schema_errors(value, TEMPLATE_CONTRACT)
    if errors:
        raise CompositionError("invalid_template", f"{TEMPLATE_CONTRACT} is invalid: {errors[0]}",
                               errors=errors)
    ids = [step["id"] for step in value["steps"]]
    if len(set(ids)) != len(ids):
        raise CompositionError("invalid_template", "step ids must be unique")
    seen: set[str] = set()
    for step in value["steps"]:
        check_range(step["range"])
        unknown = set(step.get("depends_on", [])) - seen
        if unknown:
            raise CompositionError(
                "invalid_template", f"step {step['id']} depends on later or unknown steps {sorted(unknown)}")
        seen.add(step["id"])
    if manifest is not None:
        if value["owner_pack"] != manifest["name"]:
            raise CompositionError("invalid_template", "template owner_pack must be its manifest's pack")
        required = {r["capability"] for r in manifest["requires"]}
        undeclared = sorted({s["capability"] for s in value["steps"]} - required)
        if undeclared:
            raise CompositionError(
                "undeclared_capability",
                f"template {value['template_id']} uses capabilities its pack does not require: {undeclared}",
                capabilities=undeclared,
            )
    if known_capabilities is not None:
        unknown = sorted({s["capability"] for s in value["steps"]} - set(known_capabilities))
        if unknown:
            raise CompositionError("unknown_capability", f"no provider declares {unknown}",
                                   capabilities=unknown)
    value["template_hash"] = template_hash(value)
    return value


def _keys(value: Any, path: str = ""):
    if isinstance(value, Mapping):
        for key, item in value.items():
            where = f"{path}/{key}" if path else str(key)
            yield str(key), where
            yield from _keys(item, where)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _keys(item, f"{path}[{index}]")


def templates_of(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validated workflow templates a manifest contributes."""

    return [validate_template(t, manifest=manifest)
            for t in manifest.get("contributes", {}).get("workflow_templates", [])]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:48] or "source"


def adapt_investigation_template(
    definition: Mapping[str, Any],
    *,
    template_id: str,
    version: str,
    owner_pack: str,
    origin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Express an investigation template as a workflow template.

    Every source-pack pin is kept verbatim and becomes an acquisition step on
    the shared ``sources.acquire`` capability; the report outline becomes the
    expected artifacts, recorded by an ``intake.session-artifact`` step.
    """

    pins = [dict(pin) for pin in definition.get("source_packs", [])]
    steps = []
    for pin in pins:
        steps.append({
            "id": f"acquire-{_slug(pin['pack_id'])}",
            "capability": "sources.acquire",
            "range": "^1.0.0",
            "effect": "acquisition",
            "optional": True,
            "arguments": {"pack_id": pin["pack_id"], "version": pin["version"]},
        })
    steps.append({
        "id": "record-findings",
        "capability": "intake.session-artifact",
        "range": "^1.0.0",
        "effect": "local-mutation",
        "depends_on": [step["id"] for step in steps],
        "arguments": {"questions": list(definition.get("questions", []))},
    })
    template = {
        "contract": TEMPLATE_CONTRACT,
        "template_id": template_id,
        "version": version,
        "owner_pack": owner_pack,
        "description": str(definition.get("description") or definition.get("name") or template_id),
        "parameters": {
            name: {"type": "string", "description": str(text)}
            for name, text in sorted(dict(definition.get("parameters", {})).items())
        },
        "steps": steps,
        "permitted_record_kinds": ["document"],
        "expected_artifacts": [
            {"kind": "report-section", "name": str(section), "from_step": "record-findings"}
            for section in definition.get("report_outline", [])
        ],
        "source_packs": pins,
    }
    if origin:
        template["origin"] = dict(origin)
    return validate_template(template)


def adapt_stored_investigation_templates(conn: Any, *, owner_pack: str = "research") -> list[dict[str, Any]]:
    """Adapt the current revision of every stored investigation template."""

    rows = conn.execute(
        "SELECT t.template_id, t.revision, r.state_json FROM investigation_templates t "
        "JOIN investigation_template_revisions r ON r.template_id=t.template_id AND r.revision=t.revision "
        "ORDER BY t.template_id").fetchall()
    adapted = []
    for template_id, revision, encoded in rows:
        state = json.loads(encoded)
        adapted.append(adapt_investigation_template(
            state["definition"],
            template_id=f"investigation.t-{_slug(template_id.split(':', 1)[-1])}",
            version=f"{int(revision)}.0.0",
            owner_pack=owner_pack,
            origin={"investigation_template_id": template_id, "revision": int(revision),
                    "namespace": state["namespace"]},
        ))
    return adapted


def compile_recipe(
    template: Mapping[str, Any], *, namespace: str, plan: Mapping[str, Any], consumer: str
) -> dict[str, Any]:
    """Compile a template into a research recipe bound to ``plan``.

    Each step's tool is the plan's binding target for the consumer and
    capability. Unbound optional steps keep a placeholder tool and simply have
    no adapter, so the existing runner records them as omissions.
    """

    bindings = {(b["consumer"], b["capability"]): b for b in plan["bindings"]}
    steps = []
    for step in template["steps"]:
        binding = bindings.get((consumer, step["capability"]))
        if binding is None and not step.get("optional"):
            raise CompositionError(
                "unbound_step",
                f"required step {step['id']} ({step['capability']}) has no binding in plan {plan['digest']}",
                step_id=step["id"], capability=step["capability"])
        target = None
        if binding is not None:
            registered = [b["id"] for b in binding.get("bindings", []) if b["kind"] == "registered"]
            mcp = [b["id"] for b in binding.get("bindings", []) if b["kind"] == "mcp-tool"]
            target = (registered or mcp)[0]
        steps.append({
            "id": step["id"],
            "tool": target or f"unbound:{step['capability']}",
            "input_schema": (binding or {}).get("input_contract", {"name": "unbound"}),
            "output_schema": (binding or {}).get("output_contract", {"name": "unbound"}),
            "depends_on": list(step.get("depends_on", [])),
            "optional": bool(step.get("optional", False)),
            "effect": step["effect"],
            "capability": step["capability"],
        })
    inputs = {
        name: {k: v for k, v in spec.items() if k in {"type", "required", "default", "description"}}
        for name, spec in template["parameters"].items()
    }
    return {
        "recipe_id": template["template_id"],
        "version": f"{template['version']}+{plan['digest'][7:19]}",
        "namespace": namespace,
        "inputs": inputs,
        "steps": steps,
        "outputs": [artifact["name"] for artifact in template["expected_artifacts"]],
        "compatibility": {"plan_digest": plan["digest"], "template_hash": template["template_hash"],
                          "consumer": consumer},
        "provenance": {"workflow_template": f"{template['template_id']}@{template['version']}"},
    }


class WorkflowStore:
    """Session and run bindings to plans; step receipts for reconciliation."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(DDL)

    def bind_session(
        self,
        namespace: str,
        session_id: str,
        *,
        template: Mapping[str, Any],
        plan: Mapping[str, Any],
        profile: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Record mode, profile and template for a session, beside its state."""

        from src.kb.intake_modes import IntakeStore

        session = IntakeStore(self.conn, initialize=False).inspect(
            namespace, session_id, principal_id=principal_id, scopes=scopes)
        profiles = template.get("profile")
        if profile is not None and profiles is not None and profile != profiles:
            raise CompositionError("profile_mismatch", "the session profile differs from the template's")
        row = [session_id, namespace, session["mode"], profile, template["template_id"],
               template["version"], template["template_hash"], plan["digest"], plan["resolver_version"],
               principal_id, self.now()]
        self.conn.execute("DELETE FROM composition_session_bindings WHERE session_id=?", [session_id])
        self.conn.execute("INSERT INTO composition_session_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
        return self.session_binding(session_id)

    def session_binding(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT session_id, namespace, mode, profile, template_id, template_version, template_hash, "
            "plan_digest, resolver_version FROM composition_session_bindings WHERE session_id=?",
            [session_id]).fetchone()
        if row is None:
            return None
        keys = ("session_id", "namespace", "mode", "profile", "template_id", "template_version",
                "template_hash", "plan_digest", "resolver_version")
        return dict(zip(keys, row, strict=True))

    def bind_run(self, run_id: str, *, namespace: str, recipe_revision_id: str,
                 template: Mapping[str, Any], consumer: str, plan: Mapping[str, Any],
                 preflight: Mapping[str, Any], principal_id: str, run_key: str,
                 parameters: Mapping[str, Any], network: str,
                 session_id: str | None = None, project_id: str | None = None) -> None:
        """Record the plan a run executes under, with its preflight observation."""

        existing = self.run_binding(run_id)
        if existing is not None:
            if existing["plan_digest"] != plan["digest"]:
                raise CompositionError("plan_changed", "run is bound to a different plan")
            return
        self.conn.execute(
            "INSERT INTO composition_run_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'running',?,NULL,?,?,?,?)",
            [run_id, namespace, recipe_revision_id, template["template_id"], template["version"], consumer,
             session_id, project_id, plan["digest"], plan["resolver_version"], canonical(dict(preflight)),
             principal_id, self.now(), run_key, canonical(dict(parameters)), network,
             canonical(dict(template))])

    def set_recipe_run(self, run_id: str, recipe_run_id: str) -> None:
        self.conn.execute("UPDATE composition_run_bindings SET recipe_run_id=? WHERE run_id=?",
                          [recipe_run_id, run_id])

    def run_binding(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT run_id, namespace, recipe_revision_id, template_id, template_version, consumer, "
            "session_id, project_id, plan_digest, resolver_version, preflight_json, principal_id, status, "
            "recipe_run_id, run_key, parameters_json, network, template_json "
            "FROM composition_run_bindings WHERE run_id=?", [run_id]).fetchone()
        if row is None:
            return None
        keys = ("run_id", "namespace", "recipe_revision_id", "template_id", "template_version",
                "consumer", "session_id", "project_id", "plan_digest", "resolver_version",
                "preflight", "principal_id", "status", "recipe_run_id", "run_key", "parameters",
                "network", "template")
        value = dict(zip(keys, row, strict=True))
        for key in ("preflight", "parameters", "template"):
            value[key] = json.loads(value[key])
        return value

    def set_run_status(self, run_id: str, status: str) -> None:
        self.conn.execute("UPDATE composition_run_bindings SET status=? WHERE run_id=?", [status, run_id])

    # -- step receipts (C07.4) -----------------------------------------------
    def step_receipt(self, run_id: str, step_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT idempotency_key, status, effect, binding_id, result_json, references_json "
            "FROM composition_step_receipts WHERE run_id=? AND step_id=?", [run_id, step_id]).fetchone()
        if row is None:
            return None
        return {"idempotency_key": row[0], "status": row[1], "effect": row[2], "binding_id": row[3],
                "result": None if row[4] is None else json.loads(row[4]),
                "references": json.loads(row[5])}

    def record_step(self, run_id: str, step_id: str, *, key: str, status: str, effect: str,
                    binding_id: str, result: Mapping[str, Any] | None = None,
                    references: Iterable[Mapping[str, Any]] = ()) -> None:
        self.conn.execute(
            "INSERT INTO composition_step_receipts VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (run_id, step_id) DO UPDATE SET idempotency_key=excluded.idempotency_key, "
            "status=excluded.status, result_json=excluded.result_json, "
            "references_json=excluded.references_json, updated_at_ms=excluded.updated_at_ms",
            [run_id, step_id, key, status, effect, binding_id,
             None if result is None else canonical(dict(result)),
             canonical([dict(r) for r in references]), self.now()])

    def step_receipts(self, run_id: str) -> list[dict[str, Any]]:
        return [{"step_id": row[0], "status": row[1], "effect": row[2], "binding_id": row[3]}
                for row in self.conn.execute(
                    "SELECT step_id, status, effect, binding_id FROM composition_step_receipts "
                    "WHERE run_id=? ORDER BY step_id", [run_id]).fetchall()]


def record_reference(owner_capability: str, record_kind: str, record_id: str, namespace: str,
                     revision: Any) -> dict[str, Any]:
    """A cross-pack reference: owner capability, native kind/ID, namespace and revision."""

    return {"owner_capability": owner_capability, "record_kind": record_kind, "record_id": str(record_id),
            "namespace": namespace, "revision": revision}


__all__ = [
    "DDL",
    "TEMPLATE_CONTRACT",
    "WorkflowStore",
    "adapt_investigation_template",
    "adapt_stored_investigation_templates",
    "compile_recipe",
    "record_reference",
    "template_hash",
    "templates_of",
    "validate_template",
]
