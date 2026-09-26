"""Versioned procedures, guided rehearsal runs and adapter-executed automated steps."""

from __future__ import annotations

import json
import time
from typing import Any

from src.kb.intake_modes import (
    IntakeError,
    IntakeStore,
    _bounded,
    _hash,
    _json,
    _plugin_ref_accessible,
    _reference,
    _text,
)

CONTRACT = "noesis-intake-playbook-v1"
RUN_CONTRACT = "noesis-intake-playbook-run-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS intake_playbooks(
 playbook_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 request_hash TEXT NOT NULL,revision BIGINT NOT NULL,content_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intake_playbook_revisions(
 playbook_id TEXT NOT NULL,revision BIGINT NOT NULL,content_json TEXT NOT NULL,
 PRIMARY KEY(playbook_id,revision));
CREATE TABLE IF NOT EXISTS intake_playbook_edits(
 playbook_id TEXT NOT NULL,edit_key TEXT NOT NULL,request_hash TEXT NOT NULL,
 revision BIGINT NOT NULL,PRIMARY KEY(playbook_id,edit_key));
CREATE TABLE IF NOT EXISTS intake_playbook_runs(
 run_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 request_hash TEXT NOT NULL,revision BIGINT NOT NULL,content_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intake_playbook_run_revisions(
 run_id TEXT NOT NULL,revision BIGINT NOT NULL,content_json TEXT NOT NULL,
 PRIMARY KEY(run_id,revision));
CREATE TABLE IF NOT EXISTS intake_playbook_run_commands(
 run_id TEXT NOT NULL,command_key TEXT NOT NULL,request_hash TEXT NOT NULL,
 revision BIGINT NOT NULL,PRIMARY KEY(run_id,command_key));
CREATE TABLE IF NOT EXISTS intake_playbook_step_executions(
 namespace TEXT NOT NULL,owner TEXT NOT NULL,idempotency_key TEXT NOT NULL,
 correlation_id TEXT NOT NULL,run_id TEXT NOT NULL,step_id TEXT NOT NULL,
 request_hash TEXT NOT NULL,status TEXT NOT NULL,receipt_json TEXT,
 created_at_ms BIGINT NOT NULL,updated_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace,owner,idempotency_key),UNIQUE(namespace,owner,correlation_id));
"""
AUTOMATION_CONTRACT = "noesis-intake-playbook-automation-v1"


def _strings(values: Any, field: str, *, limit: int = 50) -> list[str]:
    if not isinstance(values, list) or len(values) > limit:
        raise IntakeError("invalid_playbook", f"{field} allows up to {limit} entries")
    return [_text(value, field, limit=2000) for value in values]


def _steps(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list) or not values or len(values) > 50:
        raise IntakeError("invalid_playbook", "playbook needs 1–50 ordered steps")
    result = []
    for index, raw in enumerate(values):
        base = {"action", "expected_result", "recovery"}
        if not isinstance(raw, dict) or not base <= set(raw) <= base | {"automation"}:
            raise IntakeError(
                "invalid_playbook", "each step needs action, expected_result, and recovery"
            )
        step: dict[str, Any] = {
            "id": f"step-{index + 1}",
            "action": _text(raw["action"], "step action", limit=2000),
            "expected_result": _text(raw["expected_result"], "expected result", limit=2000),
            "recovery": _text(raw["recovery"], "failure recovery", limit=2000),
        }
        if "automation" in raw:
            automation = raw["automation"]
            if not isinstance(automation, dict) or set(automation) != {"action", "parameters"}:
                raise IntakeError("invalid_playbook", "step automation needs action and parameters")
            from src.kb.intake_problem import IntakeProblemStore

            step["automation"] = {
                "action": automation["action"],
                "parameters": IntakeProblemStore._action_parameters(
                    automation["action"], automation["parameters"]
                ),
            }
        result.append(step)
    return result


ARTIFACT_KINDS = {"playbook", "checklist", "template", "default_configuration", "automation_rule"}
PROMOTABLE_MODES = {"Problem-Solving", "Creation", "Deep Research"}


def _kind_fields(kind: str, steps: list[dict], template_body: Any, settings: Any, trigger: Any) -> dict:
    """Validate the kind-specific fields of an externalized procedure."""

    if kind not in ARTIFACT_KINDS:
        raise IntakeError("invalid_playbook", "unsupported procedure kind")
    fields: dict[str, Any] = {}
    automated = [step for step in steps if "automation" in step]
    if kind == "checklist" and automated:
        raise IntakeError("invalid_playbook", "a checklist records manual steps; use an automation rule for automation")
    if kind == "template":
        if not isinstance(template_body, str) or not template_body.strip():
            raise IntakeError("invalid_playbook", "a template needs a template_body")
        fields["template_body"] = _text(template_body, "template body", limit=50_000)
    elif template_body is not None:
        raise IntakeError("invalid_playbook", "template_body applies only to templates")
    if kind == "default_configuration":
        if not isinstance(settings, dict) or not 1 <= len(settings) <= 100 or any(
            not isinstance(key, str) or not key or len(key) > 200
            or not (value is None or isinstance(value, (bool, int, float, str)))
            or (isinstance(value, str) and len(value) > 2000)
            for key, value in settings.items()
        ):
            raise IntakeError("invalid_playbook", "settings need 1–100 scalar key/value defaults")
        fields["settings"] = dict(sorted(settings.items()))
    elif settings is not None:
        raise IntakeError("invalid_playbook", "settings apply only to default configurations")
    if kind == "automation_rule":
        if not automated:
            raise IntakeError("invalid_playbook", "an automation rule needs at least one automated step")
        if not isinstance(trigger, str) or not trigger.strip():
            raise IntakeError("invalid_playbook", "an automation rule needs a trigger")
        fields["trigger"] = _text(trigger, "rule trigger", limit=2000)
    elif trigger is not None:
        raise IntakeError("invalid_playbook", "trigger applies only to automation rules")
    return fields


class IntakePlaybookStore:
    def __init__(self, conn: Any, *, initialize=True, now=None, adapters=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        # Deployment-owned allowlisted adapters shared with Problem-Solving
        # actions; the public MCP server configures none.
        from src.kb.intake_problem import ACTION_ADAPTER_ALLOWLIST

        self.adapters = dict(adapters or {})
        if set(self.adapters) - ACTION_ADAPTER_ALLOWLIST or any(
            not callable(adapter) for adapter in self.adapters.values()
        ):
            raise IntakeError("invalid_adapter", "only callable allowlisted adapters can be configured")
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(state: dict, principal_id: str, scopes: set[str], *, write=False) -> None:
        IntakeStore._authorize(state, principal_id, scopes, write=write)
        if "operator" not in scopes and any(
            ref["namespace"] != state["namespace"]
            and f"namespace:{ref['namespace']}:read" not in scopes
            for ref in state.get("references", [])
        ):
            raise IntakeError("unauthorized", "current access to playbook sources is required")
        if any(
            not _plugin_ref_accessible(link, state["namespace"], scopes)
            for link in state.get("plugin_links", [])
        ):
            raise IntakeError("unauthorized", "current access to plugin-linked playbook sources is required")

    def _playbook(self, namespace: str, playbook_id: str, revision=None) -> dict:
        row = self.conn.execute(
            "SELECT r.content_json FROM intake_playbooks p JOIN intake_playbook_revisions r "
            "ON p.playbook_id=r.playbook_id WHERE p.namespace=? AND p.playbook_id=? "
            "AND r.revision=coalesce(?,p.revision)",
            [namespace, playbook_id, revision],
        ).fetchone()
        if row is None:
            raise IntakeError("playbook_not_found", "playbook revision is unavailable")
        return json.loads(row[0])

    def _run(self, namespace: str, run_id: str, revision=None) -> dict:
        row = self.conn.execute(
            "SELECT r.content_json FROM intake_playbook_runs p JOIN intake_playbook_run_revisions r "
            "ON p.run_id=r.run_id WHERE p.namespace=? AND p.run_id=? "
            "AND r.revision=coalesce(?,p.revision)",
            [namespace, run_id, revision],
        ).fetchone()
        if row is None:
            raise IntakeError("run_not_found", "guided run revision is unavailable")
        return json.loads(row[0])

    def promote_problem(
        self,
        namespace: str,
        problem_session_id: str,
        request_key: str,
        *,
        title: str,
        prerequisites: list[str],
        environment: str,
        steps: list[dict],
        verification: str,
        source_rationale: str,
        concept_references: list[dict] | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict:
        request_key = _text(request_key, "request_key", limit=256)
        session = IntakeStore(self.conn)._state(namespace, problem_session_id)
        IntakeStore._authorize_full_read(session, principal_id, scopes)
        if (
            session["mode"] != "Problem-Solving"
            or session["status"] != "completed"
            or session["inputs"].get("problem_contract") != "noesis-problem-trail-v1"
            or not session["data"].get("verified")
        ):
            raise IntakeError(
                "unverified_problem", "complete a typed, verified problem before promotion"
            )
        refs = list(session["references"])
        for raw in concept_references or []:
            ref = _reference(raw, namespace, scopes)
            if ref["kind"] != "concept":
                raise IntakeError("invalid_reference", "expected a concept reference")
            if ref not in refs:
                refs.append(ref)
        content = {
            "contract": CONTRACT,
            "playbook_id": "playbook:" + _hash([namespace, principal_id, request_key])[:32],
            "namespace": namespace,
            "owner": principal_id,
            "revision": 1,
            "title": _text(title, "title", limit=1000),
            "prerequisites": _strings(prerequisites, "prerequisites"),
            "environment": _text(environment, "environment", limit=2000),
            "steps": _steps(steps),
            "verification": _text(verification, "verification", limit=2000),
            "source_rationale": _text(source_rationale, "source rationale", limit=5000),
            "references": refs,
            **({"plugin_links": list(session.get("plugin_links", []))}
               if session.get("plugin_links") else {}),
            "origin": {"session_id": problem_session_id, "revision": session["revision"]},
            "trust_state": "draft",
            "created_at_ms": self.now(),
        }
        return self._store_new(content, principal_id, scopes)

    def promote_work(
        self,
        namespace: str,
        session_id: str,
        request_key: str,
        *,
        artifact_kind: str,
        title: str,
        prerequisites: list[str],
        environment: str,
        steps: list[dict],
        verification: str,
        source_rationale: str,
        template_body: str | None = None,
        settings: dict | None = None,
        trigger: str | None = None,
        concept_references: list[dict] | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict:
        """Externalize completed work as a versioned procedure of any supported kind.

        The origin must be a completed Problem-Solving (verified), Creation or
        Deep Research session. Every kind starts as a draft; trust comes only
        from a later guided rehearsal or adapter execution.
        """

        request_key = _text(request_key, "request_key", limit=256)
        session = IntakeStore(self.conn)._state(namespace, session_id)
        IntakeStore._authorize_full_read(session, principal_id, scopes)
        creation_origin = (
            session["mode"] == "Externalization"
            and session["status"] in {"active", "paused", "completed"}
            and isinstance(session["inputs"].get("creation_project_id"), str)
        )
        if creation_origin:
            # An Externalization session opened by a finished Creation handoff:
            # the finished, reviewed artifact is the completed work.
            from src.kb.intake_creation import IntakeCreationStore

            project = IntakeCreationStore(self.conn, initialize=False).inspect(
                namespace, session["inputs"]["creation_project_id"],
                principal_id=principal_id, scopes=scopes,
            )
            if (project["status"] != "finished"
                    or project["revision"] != session["inputs"].get("creation_project_revision")):
                raise IntakeError("incomplete_origin", "the handed-off Creation revision is no longer finished and current")
        elif session["mode"] not in PROMOTABLE_MODES or session["status"] != "completed":
            raise IntakeError(
                "incomplete_origin",
                "externalize a completed Problem-Solving, Creation, or Deep Research session, "
                "or an Externalization session handed off from a finished Creation",
            )
        if session["mode"] == "Problem-Solving" and not session["data"].get("verified"):
            raise IntakeError("unverified_problem", "complete a verified problem before externalizing it")
        parsed_steps = _steps(steps)
        kind_fields = _kind_fields(artifact_kind, parsed_steps, template_body, settings, trigger)
        refs = list(session["references"])
        for raw in concept_references or []:
            ref = _reference(raw, namespace, scopes)
            if ref["kind"] != "concept":
                raise IntakeError("invalid_reference", "expected a concept reference")
            if ref not in refs:
                refs.append(ref)
        content = {
            "contract": CONTRACT,
            "playbook_id": "playbook:" + _hash([namespace, principal_id, request_key])[:32],
            "namespace": namespace,
            "owner": principal_id,
            "revision": 1,
            "artifact_kind": artifact_kind,
            "title": _text(title, "title", limit=1000),
            "prerequisites": _strings(prerequisites, "prerequisites"),
            "environment": _text(environment, "environment", limit=2000),
            "steps": parsed_steps,
            "verification": _text(verification, "verification", limit=2000),
            "source_rationale": _text(source_rationale, "source rationale", limit=5000),
            **kind_fields,
            "references": refs,
            **({"plugin_links": list(session.get("plugin_links", []))}
               if session.get("plugin_links") else {}),
            "origin": {"session_id": session_id, "revision": session["revision"], "mode": session["mode"]},
            "trust_state": "draft",
            "created_at_ms": self.now(),
        }
        return self._store_new(content, principal_id, scopes)

    def _store_new(self, content: dict, principal_id: str, scopes: set[str]) -> dict:
        self._authorize(content, principal_id, scopes, write=True)
        content = _bounded(content)
        namespace = content["namespace"]
        digest = _hash({key: value for key, value in content.items() if key != "created_at_ms"})
        prior = self.conn.execute(
            "SELECT request_hash FROM intake_playbooks WHERE playbook_id=?",
            [content["playbook_id"]],
        ).fetchone()
        if prior:
            if prior[0] != digest:
                raise IntakeError("idempotency_conflict", "request_key identifies another playbook")
            current = self._playbook(namespace, content["playbook_id"])
            self._authorize(current, principal_id, scopes)
            return {**current, "idempotent": True}
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO intake_playbooks VALUES (?,?,?,?,?,?)",
                [content["playbook_id"], namespace, principal_id, digest, 1, _json(content)],
            )
            self.conn.execute(
                "INSERT INTO intake_playbook_revisions VALUES (?,?,?)",
                [content["playbook_id"], 1, _json(content)],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**content, "idempotent": False}

    def inspect(
        self, namespace: str, playbook_id: str, *, principal_id: str,
        scopes: set[str], revision: int | None = None,
    ) -> dict:
        current = self._playbook(namespace, playbook_id)
        self._authorize(current, principal_id, scopes)
        value = current if revision is None else self._playbook(namespace, playbook_id, revision)
        self._authorize(value, principal_id, scopes)
        completed = self.conn.execute(
            "SELECT run_id,content_json FROM intake_playbook_runs "
            "WHERE namespace=? AND owner=? "
            "AND json_extract_string(content_json,'$.playbook_id')=? "
            "AND CAST(json_extract_string(content_json,'$.playbook_revision') AS INTEGER)=? "
            "AND json_extract_string(content_json,'$.status')='completed' "
            "ORDER BY CAST(json_extract_string(content_json,'$.updated_at_ms') AS BIGINT) DESC,run_id DESC LIMIT 1001",
            [namespace, principal_id, playbook_id, value["revision"]],
        ).fetchall()
        if len(completed) > 1000:
            raise IntakeError("rehearsal_budget_exceeded", "more than 1000 completed rehearsals are linked to this revision")
        evidence = None
        if completed:
            run_id, raw = completed[0]
            run = json.loads(raw)
            verification = run.get("verification") or {}
            if verification.get("passed") is True:
                automated = [step["id"] for step in value["steps"] if "automation" in step]
                receipts = {
                    item["step_id"]: item["execution_receipt"]
                    for item in run.get("observations", [])
                    if item.get("passed") and item.get("execution_receipt")
                }
                executed = bool(automated) and all(
                    receipts.get(step_id, {}).get("status") == "completed" for step_id in automated
                )
                evidence = {
                    "run_id": run_id,
                    "run_revision": run["revision"],
                    "playbook_revision": run["playbook_revision"],
                    "environment": run["environment"],
                    "verified_at_ms": verification["at_ms"],
                    "observation": verification["observation"],
                    "basis": (
                        "adapter_executed_steps_and_reported_verification"
                        if executed else "caller_reported_guided_rehearsal"
                    ),
                    **({"execution_receipts": [
                        {"step_id": step_id, "idempotency_key": receipts[step_id]["idempotency_key"],
                         "action": receipts[step_id]["action"]}
                        for step_id in automated
                    ]} if executed else {}),
                }
        trust_state = value["trust_state"]
        if evidence:
            trust_state = (
                "executed_verified"
                if evidence["basis"].startswith("adapter_executed") else "rehearsed_reported"
            )
        return {
            **value,
            "reported_rehearsal_count": len(completed),
            "trust_state": trust_state,
            "trust_evidence": evidence,
        }

    def preview_step_automation(
        self, namespace: str, run_id: str, step_id: str, *, principal_id: str, scopes: set[str],
    ) -> dict:
        """Show the exact automated step and adapter availability without executing it."""

        run = self._run(namespace, run_id)
        self._authorize(run, principal_id, scopes)
        playbook = self._playbook(namespace, run["playbook_id"], run["playbook_revision"])
        self._authorize(playbook, principal_id, scopes)
        step = next((item for item in playbook["steps"] if item["id"] == step_id), None)
        if step is None or "automation" not in step:
            raise IntakeError("not_automated", "the step has no configured automation")
        body = {
            "contract": AUTOMATION_CONTRACT, "run_id": run_id, "run_revision": run["revision"],
            "playbook_id": playbook["playbook_id"], "playbook_revision": playbook["revision"],
            "step_id": step_id, "automation": step["automation"],
            "adapter_available": step["automation"]["action"] in self.adapters,
            "is_next_step": playbook["steps"][run["next_step"]]["id"] == step_id
            if run["next_step"] < len(playbook["steps"]) else False,
        }
        return {**body, "preview_hash": _hash(body)}

    def execute_step_automation(
        self, namespace: str, run_id: str, step_id: str, *, expected_revision: int,
        preview_hash: str, idempotency_key: str, correlation_id: str,
        principal_id: str, scopes: set[str],
    ) -> dict:
        """Execute the next automated guided-run step once through its adapter.

        A durable reservation precedes the adapter call, so a retry after a
        crash returns the reservation or receipt and never calls the adapter
        again. A completed call records a passing step with the receipt; an
        uncertain call records a failed step that needs manual recovery.
        """

        idempotency_key = _text(idempotency_key, "idempotency_key", limit=256)
        correlation_id = _text(correlation_id, "correlation_id", limit=256)
        request_hash = _hash([namespace, principal_id, run_id, step_id, expected_revision, correlation_id])
        existing = self.conn.execute(
            "SELECT request_hash,status,receipt_json FROM intake_playbook_step_executions "
            "WHERE namespace=? AND owner=? AND idempotency_key=?",
            [namespace, principal_id, idempotency_key],
        ).fetchone()
        if existing:
            if existing[0] != request_hash:
                raise IntakeError("idempotency_conflict", "idempotency key identifies another step execution")
            return {"contract": AUTOMATION_CONTRACT, "status": existing[1],
                    "receipt": json.loads(existing[2]) if existing[2] else None, "idempotent": True}
        preview = self.preview_step_automation(namespace, run_id, step_id, principal_id=principal_id, scopes=scopes)
        run = self._run(namespace, run_id)
        self._authorize(run, principal_id, scopes, write=True)
        if preview["preview_hash"] != preview_hash or run["revision"] != expected_revision:
            raise IntakeError("preview_stale", "run or step changed; preview again before executing")
        if run["status"] != "active" or not preview["is_next_step"]:
            raise IntakeError("wrong_step", "execute the next step of an active run")
        automation = preview["automation"]
        adapter = self.adapters.get(automation["action"])
        if adapter is None:
            return {"contract": AUTOMATION_CONTRACT, "status": "unavailable",
                    "reason": "adapter_unavailable", "executed": False, "step_id": step_id}
        now = self.now()
        self.conn.execute(
            "INSERT INTO intake_playbook_step_executions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, principal_id, idempotency_key, correlation_id, run_id, step_id,
             request_hash, "reserved", None, now, now],
        )
        try:
            result = _bounded(adapter(dict(automation["parameters"]), idempotency_key=idempotency_key,
                                      correlation_id=correlation_id), limit=64_000)
            if not isinstance(result, dict):
                raise IntakeError("invalid_adapter_result", "adapter must return a JSON object")
            status, error_code = "completed", None
        except Exception as exc:  # the external outcome may be uncertain
            result, status = None, "indeterminate"
            error_code = getattr(exc, "code", "adapter_call_uncertain")
        receipt = {
            "contract": AUTOMATION_CONTRACT, "status": status, "run_id": run_id,
            "step_id": step_id, "action": automation["action"],
            "idempotency_key": idempotency_key, "correlation_id": correlation_id,
            "adapter_result": result, "error_code": error_code, "recorded_at_ms": self.now(),
        }
        self.conn.execute(
            "UPDATE intake_playbook_step_executions SET status=?,receipt_json=?,updated_at_ms=? "
            "WHERE namespace=? AND owner=? AND idempotency_key=?",
            [status, _json(receipt), self.now(), namespace, principal_id, idempotency_key],
        )
        updated = self.command_run(
            namespace, run_id, "automation:" + _hash(idempotency_key)[:32],
            expected_revision=expected_revision, action="step",
            payload={"step_id": step_id, "passed": status == "completed",
                     "observation": f"Adapter {automation['action']} {status}"},
            principal_id=principal_id, scopes=scopes, _execution_receipt=receipt,
        )
        return {"contract": AUTOMATION_CONTRACT, "status": status, "receipt": receipt,
                "run": updated, "idempotent": False}

    def revise(
        self, namespace: str, playbook_id: str, edit_key: str, *,
        expected_revision: int, title: str, prerequisites: list[str],
        environment: str, steps: list[dict], verification: str,
        source_rationale: str, principal_id: str, scopes: set[str],
        iteration_receipt: dict | None = None,
        _within_transaction: bool = False,
    ) -> dict:
        edit_key = _text(edit_key, "edit_key", limit=256)
        if type(expected_revision) is not int or expected_revision < 1:
            raise IntakeError("invalid_revision", "expected_revision must be positive")
        patch = {
            "title": _text(title, "title", limit=1000),
            "prerequisites": _strings(prerequisites, "prerequisites"),
            "environment": _text(environment, "environment", limit=2000),
            "steps": _steps(steps),
            "verification": _text(verification, "verification", limit=2000),
            "source_rationale": _text(source_rationale, "source rationale", limit=5000),
        }
        if iteration_receipt is not None:
            if not isinstance(iteration_receipt, dict) or iteration_receipt.get("contract") != "noesis-intake-iteration-playbook-v1":
                raise IntakeError("invalid_iteration", "a versioned iteration receipt is required")
            iteration_receipt = _bounded(iteration_receipt)
        digest = _hash([patch, iteration_receipt]) if iteration_receipt is not None else _hash(patch)
        if not _within_transaction:
            self.conn.execute("BEGIN")
        try:
            current = self._playbook(namespace, playbook_id)
            self._authorize(current, principal_id, scopes, write=True)
            replay = self.conn.execute(
                "SELECT request_hash,revision FROM intake_playbook_edits "
                "WHERE playbook_id=? AND edit_key=?", [playbook_id, edit_key]
            ).fetchone()
            if replay:
                if replay[0] != digest:
                    raise IntakeError("idempotency_conflict", "edit_key identifies another revision")
                value = self._playbook(namespace, playbook_id, int(replay[1]))
                self._authorize(value, principal_id, scopes)
                if not _within_transaction:
                    self.conn.execute("COMMIT")
                return {**value, "idempotent": True}
            if current["revision"] != expected_revision:
                raise IntakeError("revision_conflict", "playbook changed; inspect before editing")
            _kind_fields(
                current.get("artifact_kind", "playbook"), patch["steps"],
                current.get("template_body"), current.get("settings"), current.get("trigger"),
            )
            value = {**current, **patch, "revision": current["revision"] + 1,
                     "trust_state": "draft", "updated_at_ms": self.now()}
            if iteration_receipt is not None:
                value["iteration_history"] = [
                    *current.get("iteration_history", []), iteration_receipt,
                ]
                if len(value["iteration_history"]) > 100:
                    raise IntakeError("iteration_limit", "playbook has 100 iteration receipts")
            _bounded(value)
            changed = self.conn.execute(
                "UPDATE intake_playbooks SET revision=?,content_json=? "
                "WHERE playbook_id=? AND revision=? RETURNING revision",
                [value["revision"], _json(value), playbook_id, expected_revision],
            ).fetchone()
            if not changed:
                raise IntakeError("revision_conflict", "concurrent playbook edit; inspect")
            self.conn.execute(
                "INSERT INTO intake_playbook_revisions VALUES (?,?,?)",
                [playbook_id, value["revision"], _json(value)],
            )
            self.conn.execute(
                "INSERT INTO intake_playbook_edits VALUES (?,?,?,?)",
                [playbook_id, edit_key, digest, value["revision"]],
            )
            if not _within_transaction:
                self.conn.execute("COMMIT")
        except Exception:
            if not _within_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return {**value, "idempotent": False}

    def start_run(
        self, namespace: str, playbook_id: str, request_key: str, *,
        playbook_revision: int, environment: str, principal_id: str,
        scopes: set[str],
    ) -> dict:
        request_key = _text(request_key, "request_key", limit=256)
        environment = _text(environment, "run environment", limit=2000)
        run_id = "playbook-run:" + _hash([namespace, principal_id, request_key])[:32]
        request = [playbook_id, playbook_revision, environment]
        digest = _hash(request)
        prior = self.conn.execute(
            "SELECT request_hash FROM intake_playbook_runs WHERE run_id=?", [run_id]
        ).fetchone()
        if prior:
            if prior[0] != digest:
                raise IntakeError("idempotency_conflict", "request_key identifies another guided run")
            value = self._run(namespace, run_id)
            self._authorize(value, principal_id, scopes)
            return {**value, "idempotent": True}
        current = self._playbook(namespace, playbook_id)
        self._authorize(current, principal_id, scopes, write=True)
        if current["revision"] != playbook_revision:
            raise IntakeError("revision_conflict", "playbook changed; inspect before running")
        value = {
            "contract": RUN_CONTRACT,
            "run_id": run_id,
            "namespace": namespace,
            "owner": principal_id,
            "playbook_id": playbook_id,
            "playbook_revision": playbook_revision,
            "references": current["references"],
            "environment": environment,
            "revision": 1,
            "status": "active",
            "next_step": 0,
            "observations": [],
            "verification": None,
            "created_at_ms": self.now(),
        }
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO intake_playbook_runs VALUES (?,?,?,?,?,?)",
                [run_id, namespace, principal_id, digest, 1, _json(value)],
            )
            self.conn.execute(
                "INSERT INTO intake_playbook_run_revisions VALUES (?,?,?)",
                [run_id, 1, _json(value)],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**value, "idempotent": False}

    def inspect_run(
        self, namespace: str, run_id: str, *, principal_id: str, scopes: set[str]
    ) -> dict:
        value = self._run(namespace, run_id)
        self._authorize(value, principal_id, scopes)
        self.inspect(
            namespace, value["playbook_id"], revision=value["playbook_revision"],
            principal_id=principal_id, scopes=scopes,
        )
        return value

    def command_run(
        self, namespace: str, run_id: str, command_key: str, *,
        expected_revision: int, action: str, payload: dict | None,
        principal_id: str, scopes: set[str],
        _execution_receipt: dict | None = None,
    ) -> dict:
        command_key = _text(command_key, "command_key", limit=256)
        if type(expected_revision) is not int or expected_revision < 1:
            raise IntakeError("invalid_revision", "expected_revision must be positive")
        if action not in {"step", "verify", "pause", "resume"}:
            raise IntakeError("invalid_action", "unsupported guided-run command")
        payload = _bounded(payload or {})
        if not isinstance(payload, dict):
            raise IntakeError("invalid_input", "payload must be an object")
        digest = _hash([action, payload])
        self.conn.execute("BEGIN")
        try:
            value = self._run(namespace, run_id)
            self._authorize(value, principal_id, scopes, write=True)
            playbook = self._playbook(namespace, value["playbook_id"], value["playbook_revision"])
            self._authorize(playbook, principal_id, scopes)
            replay = self.conn.execute(
                "SELECT request_hash,revision FROM intake_playbook_run_commands "
                "WHERE run_id=? AND command_key=?", [run_id, command_key]
            ).fetchone()
            if replay:
                if replay[0] != digest:
                    raise IntakeError("idempotency_conflict", "command_key identifies another action")
                prior = self._run(namespace, run_id, int(replay[1]))
                self.conn.execute("COMMIT")
                return {**prior, "idempotent": True}
            if value["revision"] != expected_revision:
                raise IntakeError("revision_conflict", "run changed; inspect before retry")
            if value["status"] == "completed":
                raise IntakeError("closed_run", "completed run cannot change")
            if action == "pause":
                if payload or value["status"] != "active":
                    raise IntakeError("invalid_status", "only an active run can pause")
                value["status"] = "paused"
            elif action == "resume":
                if payload or value["status"] != "paused":
                    raise IntakeError("invalid_status", "only a paused run can resume")
                value["status"] = "active"
            elif action == "step":
                if value["status"] != "active":
                    raise IntakeError("invalid_status", "resume before recording a step")
                if set(payload) != {"step_id", "passed", "observation"}:
                    raise IntakeError("invalid_input", "step needs ID, passed, and observation")
                index = value["next_step"]
                if index >= len(playbook["steps"]) or payload["step_id"] != playbook["steps"][index]["id"]:
                    raise IntakeError("wrong_step", "record the next playbook step")
                if type(payload["passed"]) is not bool:
                    raise IntakeError("invalid_input", "passed must be boolean")
                observation = _text(payload["observation"], "observed result")
                step = playbook["steps"][index]
                if "automation" in step and _execution_receipt is None and payload["passed"]:
                    raise IntakeError(
                        "automation_required",
                        "an automated step passes only through its execution receipt; "
                        "report a failed manual attempt or execute the automation",
                    )
                entry = {
                    "step_id": payload["step_id"], "passed": payload["passed"],
                    "observation": observation, "at_ms": self.now(),
                    "basis": "adapter_execution" if _execution_receipt else "caller_reported",
                }
                if _execution_receipt is not None:
                    entry["execution_receipt"] = _execution_receipt
                value["observations"].append(entry)
                if payload["passed"]:
                    value["next_step"] += 1
            else:
                if value["status"] != "active" or value["next_step"] != len(playbook["steps"]):
                    raise IntakeError("incomplete_run", "complete all steps before verification")
                if set(payload) != {"passed", "observation"} or type(payload["passed"]) is not bool:
                    raise IntakeError("invalid_input", "verification needs passed and observation")
                value["verification"] = {
                    "passed": payload["passed"],
                    "observation": _text(payload["observation"], "verification observation"),
                    "at_ms": self.now(),
                }
                if payload["passed"]:
                    value["status"] = "completed"
            value["revision"] += 1
            value["updated_at_ms"] = self.now()
            changed = self.conn.execute(
                "UPDATE intake_playbook_runs SET revision=?,content_json=? "
                "WHERE run_id=? AND revision=? RETURNING revision",
                [value["revision"], _json(value), run_id, expected_revision],
            ).fetchone()
            if not changed:
                raise IntakeError("revision_conflict", "concurrent run update; inspect")
            self.conn.execute(
                "INSERT INTO intake_playbook_run_revisions VALUES (?,?,?)",
                [run_id, value["revision"], _json(value)],
            )
            self.conn.execute(
                "INSERT INTO intake_playbook_run_commands VALUES (?,?,?,?)",
                [run_id, command_key, digest, value["revision"]],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**value, "idempotent": False}
