"""Versioned procedures and guided, caller-reported rehearsal runs."""

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
"""


def _strings(values: Any, field: str, *, limit: int = 50) -> list[str]:
    if not isinstance(values, list) or len(values) > limit:
        raise IntakeError("invalid_playbook", f"{field} allows up to {limit} entries")
    return [_text(value, field, limit=2000) for value in values]


def _steps(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list) or not values or len(values) > 50:
        raise IntakeError("invalid_playbook", "playbook needs 1–50 ordered steps")
    result = []
    for index, raw in enumerate(values):
        if not isinstance(raw, dict) or set(raw) != {
            "action", "expected_result", "recovery"
        }:
            raise IntakeError(
                "invalid_playbook", "each step needs action, expected_result, and recovery"
            )
        result.append({
            "id": f"step-{index + 1}",
            "action": _text(raw["action"], "step action", limit=2000),
            "expected_result": _text(raw["expected_result"], "expected result", limit=2000),
            "recovery": _text(raw["recovery"], "failure recovery", limit=2000),
        })
    return result


class IntakePlaybookStore:
    def __init__(self, conn: Any, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
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
            "origin": {"session_id": problem_session_id, "revision": session["revision"]},
            "trust_state": "draft",
            "created_at_ms": self.now(),
        }
        self._authorize(content, principal_id, scopes, write=True)
        content = _bounded(content)
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
            "SELECT count(*) FROM intake_playbook_runs WHERE namespace=? AND owner=? "
            "AND json_extract_string(content_json,'$.playbook_id')=? "
            "AND CAST(json_extract_string(content_json,'$.playbook_revision') AS INTEGER)=? "
            "AND json_extract_string(content_json,'$.status')='completed'",
            [namespace, principal_id, playbook_id, value["revision"]],
        ).fetchone()[0]
        return {**value, "reported_rehearsal_count": int(completed)}

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
                value["observations"].append({
                    "step_id": payload["step_id"], "passed": payload["passed"],
                    "observation": observation, "at_ms": self.now(),
                })
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
