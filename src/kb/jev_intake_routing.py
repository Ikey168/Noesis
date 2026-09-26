"""Optional source-bound free-text intent suggestions for deterministic intake routing."""

from __future__ import annotations

import time
from typing import Any

from src.kb.intake_modes import IntakeError, MODES, ROUTING_QUESTIONS, route_mode
from src.kb.research_projects import _hash

CONTRACT = "noesis-jev-intake-route-suggestion-v1"
TASK = "jev-intake-routing-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS jev_intake_intents(
 input_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 request_key TEXT NOT NULL,version BIGINT NOT NULL,content_hash TEXT NOT NULL,
 intent_text TEXT NOT NULL,updated_at_ms BIGINT NOT NULL,
 UNIQUE(namespace,owner,request_key));
CREATE TABLE IF NOT EXISTS jev_intake_intent_revisions(
 input_id TEXT NOT NULL,version BIGINT NOT NULL,content_hash TEXT NOT NULL,
 intent_text TEXT NOT NULL,created_at_ms BIGINT NOT NULL,PRIMARY KEY(input_id,version));
"""


class IntentStore:
    def __init__(self, conn: Any, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _auth(namespace, principal_id, scopes, *, write=False):
        required = "knowledge:intake:write" if write else "knowledge:intake:read"
        ns = f"namespace:{namespace}:{'write' if write else 'read'}"
        if not principal_id or "operator" not in scopes and (
            required not in scopes and (write or "knowledge:intake:write" not in scopes)
            or ns not in scopes and f"namespace:{namespace}:write" not in scopes):
            raise IntakeError("unauthorized", "current intake and namespace access required")

    def register(self, namespace, request_key, intent_text, *, principal_id, scopes):
        self._auth(namespace, principal_id, scopes, write=True)
        if (not isinstance(request_key, str) or not 1 <= len(request_key) <= 200 or
            not isinstance(intent_text, str) or not 1 <= len(intent_text.strip()) <= 8_000):
            raise IntakeError("invalid_intent", "bounded request key and free-text intent required")
        identity = "intent:" + _hash([namespace, principal_id, request_key])[:24]
        digest = _hash(intent_text.strip())
        prior = self.conn.execute("SELECT content_hash FROM jev_intake_intents WHERE input_id=?", [identity]).fetchone()
        if prior:
            if prior[0] != digest:
                raise IntakeError("idempotency_conflict", "request key identifies different intent text")
            return {**self.inspect(namespace, identity, principal_id=principal_id, scopes=scopes), "idempotent": True}
        now = self.now()
        self.conn.execute("INSERT INTO jev_intake_intents VALUES (?,?,?,?,1,?,?,?)",
                          [identity, namespace, principal_id, request_key, digest, intent_text.strip(), now])
        self.conn.execute("INSERT INTO jev_intake_intent_revisions VALUES (?,1,?,?,?)",
                          [identity, digest, intent_text.strip(), now])
        return {"input_id": identity, "namespace": namespace, "owner": principal_id,
                "version": 1, "content_hash": digest, "intent_text": intent_text.strip(),
                "idempotent": False}

    def revise(self, namespace, input_id, expected_version, intent_text, *, principal_id, scopes):
        self._auth(namespace, principal_id, scopes, write=True)
        current = self.inspect(namespace, input_id, principal_id=principal_id, scopes=scopes)
        if type(expected_version) is not int or current["version"] != expected_version:
            raise IntakeError("revision_conflict", "intent version changed")
        if not isinstance(intent_text, str) or not 1 <= len(intent_text.strip()) <= 8_000:
            raise IntakeError("invalid_intent", "bounded free-text intent required")
        digest, now = _hash(intent_text.strip()), self.now()
        changed = self.conn.execute(
            "UPDATE jev_intake_intents SET version=version+1,content_hash=?,intent_text=?,updated_at_ms=? "
            "WHERE input_id=? AND version=? RETURNING version",
            [digest, intent_text.strip(), now, input_id, expected_version]).fetchone()
        if not changed:
            raise IntakeError("revision_conflict", "intent version changed")
        self.conn.execute("INSERT INTO jev_intake_intent_revisions VALUES (?,?,?,?,?)",
                          [input_id, changed[0], digest, intent_text.strip(), now])
        return self.inspect(namespace, input_id, principal_id=principal_id, scopes=scopes)

    def inspect(self, namespace, input_id, *, principal_id, scopes):
        self._auth(namespace, principal_id, scopes)
        row = self.conn.execute(
            "SELECT owner,version,content_hash,intent_text FROM jev_intake_intents WHERE namespace=? AND input_id=?",
            [namespace, input_id]).fetchone()
        if not row or "operator" not in scopes and row[0] != principal_id:
            raise IntakeError("intent_unavailable", "intent is unavailable")
        return {"input_id": input_id, "namespace": namespace, "owner": row[0],
                "version": int(row[1]), "content_hash": row[2], "intent_text": row[3]}

    def resolve(self, *, namespace, principal_id, scopes, reference):
        current = self.inspect(namespace, reference.get("input_id"),
                               principal_id=principal_id, scopes=scopes)
        return {"version": str(current["version"]), "content_hash": current["content_hash"],
                "content": current["intent_text"]}


def suggest_intake_route(runtime, intents: IntentStore, namespace, run_id, *,
                         principal_id, scopes, input_id=None, answers=None, override=None,
                         allow_remote=False, policy=None, max_attempts=1,
                         max_cost_usd_micros=0, deadline_s=30):
    """Use hosted interpretation only when no explicit routing answer is supplied."""
    if answers is not None or override is not None:
        routed = route_mode(answers if answers is not None else {}, override=override)
        return {"contract": CONTRACT, "status": "explicit", "accepted": True,
                "route": routed, "answers": answers if answers is not None else {},
                "decision_run": None, "hosted_inference_used": False,
                "input_id": input_id}
    if not isinstance(input_id, str) or not input_id:
        raise IntakeError("invalid_intent", "versioned intent required for free-text routing")
    intent = intents.inspect(namespace, input_id, principal_id=principal_id, scopes=scopes)
    questions = {key: {"kind": "noul", "instructions": f"Does the user's expressed intent call for {mode}? Answer unknown if ambiguous."}
                 for key, mode in ROUTING_QUESTIONS}
    source_ref = {"input_id": input_id, "input_version": str(intent["version"]),
                  "content_hash": intent["content_hash"]}
    run = runtime.run(namespace, run_id, TASK,
                      state={"input_id": input_id, "intent_version": intent["version"],
                             "supported_modes": list(MODES)},
                      questions=questions, source_refs=[source_ref],
                      principal_id=principal_id, scopes=scopes,
                      allow_remote=allow_remote, policy=policy,
                      max_attempts=max_attempts, max_cost_usd_micros=max_cost_usd_micros,
                      deadline_s=deadline_s)
    current = intents.inspect(namespace, input_id, principal_id=principal_id, scopes=scopes)
    if current["version"] != intent["version"] or current["content_hash"] != intent["content_hash"]:
        raise IntakeError("source_changed", "intent changed during routing suggestion")
    receipt = (run.get("receipt") or {}).get("answers") or {}
    parsed = {key: value.get("value") for key, value in receipt.items()
              if key in questions and value.get("status") == "answered" and type(value.get("value")) is bool}
    complete = (run.get("status") == "completed" and run.get("rollout_mode") != "shadow"
                and set(parsed) == set(questions))
    return {"contract": CONTRACT, "status": "suggested" if complete else "pending",
            "accepted": False, "route": route_mode(parsed) if complete else None,
            "answers": parsed, "decision_run": run,
            "hosted_inference_used": run.get("hosted_inference_used", False),
            "input_id": input_id, "input_version": intent["version"],
            "content_hash": intent["content_hash"]}
