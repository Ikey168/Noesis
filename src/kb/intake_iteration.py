"""Measured playbook iteration with accepted changes in playbook revision history."""

from __future__ import annotations

import time
from typing import Any

from src.kb.intake_modes import (
    IntakeError,
    IntakeStore,
    _bounded,
    _hash,
    _reference,
    _text,
)
from src.kb.intake_playbooks import IntakePlaybookStore, _steps, _strings

CONTRACT = "noesis-intake-iteration-playbook-v1"


class IntakeIterationStore:
    def __init__(self, conn: Any, *, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))

    def start(
        self, namespace: str, request_key: str, *, playbook_id: str,
        expected_revision: int, expected: str, stability_criteria: str,
        intent: str, principal_id: str, scopes: set[str],
        origin_session_id: str | None = None,
    ) -> dict:
        existing_id = "intake:" + _hash([namespace, principal_id, request_key])[:32]
        ledger = IntakeStore(self.conn, now=self.now)
        try:
            existing = ledger._state(namespace, existing_id)
        except IntakeError as exc:
            if exc.code != "session_not_found":
                raise
            existing = None
        if existing is None:
            playbook = IntakePlaybookStore(self.conn, now=self.now).inspect(
                namespace, playbook_id, principal_id=principal_id, scopes=scopes,
            )
            if playbook["revision"] != expected_revision:
                raise IntakeError("revision_conflict", "playbook changed; inspect before iterating")
        target = {"kind": "playbook", "id": playbook_id,
                  "namespace": namespace, "version": expected_revision}
        return ledger.create(
            namespace, "Iteration", request_key, intent=intent,
            inputs={"iteration_contract": CONTRACT, "playbook": target,
                    "expected": _text(expected, "expected outcome", limit=2000),
                    "stability_criteria": _text(stability_criteria, "stability criteria", limit=2000)},
            references=[target],
            origin={"session_id": origin_session_id, "reason": "New feedback on playbook"}
            if origin_session_id else None,
            principal_id=principal_id, scopes=scopes,
        )

    def record_outcome(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, observed: str, learning: str,
        measurements: list[dict], uncertainty: str, external_causes: str,
        evidence: list[dict], principal_id: str, scopes: set[str],
    ) -> dict:
        observed = _text(observed, "observed outcome", limit=2000)
        learning = _text(learning, "learning", limit=2000)
        uncertainty = _text(uncertainty, "uncertainty", limit=2000)
        external_causes = _text(external_causes, "external causes", limit=2000)
        if not isinstance(measurements, list) or not 1 <= len(measurements) <= 20:
            raise IntakeError("invalid_measurement", "record one to 20 observed measurements")
        measured = []
        for raw in measurements:
            if not isinstance(raw, dict) or set(raw) != {"metric", "expected", "observed", "unit"}:
                raise IntakeError("invalid_measurement", "metric, expected, observed, and unit are required")
            measured.append({key: _text(raw[key], key, limit=500) for key in raw})
        if not isinstance(evidence, list) or len(evidence) > 20:
            raise IntakeError("invalid_reference", "at most 20 evidence references")
        refs = [_reference(raw, namespace, scopes) for raw in evidence]
        outcome = {"observed": observed, "learning": learning, "measurements": measured,
                   "uncertainty": uncertainty, "external_causes": external_causes,
                   "evidence": refs, "basis": "caller_reported"}

        def apply(state: dict, _payload: dict) -> None:
            self._typed(state)
            if state["data"].get("proposal"):
                raise IntakeError("proposal_exists", "review or reset the existing proposal before changing observations")
            state["data"].update({"expected": state["inputs"]["expected"],
                                  "observed": observed, "learning": learning,
                                  "outcome": {**outcome, "at_ms": self.now()}})
            state["data"].pop("iteration_command", None)

        return IntakeStore(self.conn, now=self.now).command(
            namespace, session_id, command_key, expected_revision=expected_revision,
            action="record", payload={"data": {"iteration_command": outcome},
                                      "references": refs},
            principal_id=principal_id, scopes=scopes, record_hook=apply,
        )

    def propose(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, title: str, prerequisites: list[str],
        environment: str, steps: list[dict], verification: str,
        source_rationale: str, before_after_rationale: str,
        principal_id: str, scopes: set[str],
    ) -> dict:
        content = {"title": _text(title, "title", limit=1000),
                   "prerequisites": _strings(prerequisites, "prerequisites"),
                   "environment": _text(environment, "environment", limit=2000),
                   "steps": _steps(steps),
                   "verification": _text(verification, "verification", limit=2000),
                   "source_rationale": _text(source_rationale, "source rationale", limit=5000)}
        rationale = _text(before_after_rationale, "before/after rationale", limit=5000)
        proposal = _bounded({"content": content, "before_after_rationale": rationale,
                             "review_state": "proposed", "basis": "caller_authored"})

        def apply(state: dict, _payload: dict) -> None:
            self._typed(state)
            if not state["data"].get("outcome"):
                raise IntakeError("outcome_required", "record observed measurements before proposing a change")
            if state["data"].get("accepted_revision"):
                raise IntakeError("already_accepted", "start a new cycle for another revision")
            state["data"]["proposal"] = {**proposal,
                                           "proposed_at_ms": self.now(),
                                           "proposal_revision": state["revision"] + 1}
            state["data"].pop("iteration_command", None)

        return IntakeStore(self.conn, now=self.now).command(
            namespace, session_id, command_key, expected_revision=expected_revision,
            action="record", payload={"data": {"iteration_command": proposal}},
            principal_id=principal_id, scopes=scopes, record_hook=apply,
        )

    def accept(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, principal_id: str, scopes: set[str],
    ) -> dict:
        ledger = IntakeStore(self.conn, now=self.now)
        state = ledger._state(namespace, session_id)
        ledger._authorize_full_read(state, principal_id, scopes)
        self._typed(state)
        replay = self.conn.execute(
            "SELECT revision FROM intake_session_commands WHERE session_id=? AND command_key=?",
            [session_id, command_key],
        ).fetchone()
        if replay:
            recorded = ledger._state(namespace, session_id, int(replay[0]))
            if int(replay[0]) != expected_revision + 1 or not recorded["data"].get("accepted_revision"):
                raise IntakeError("idempotency_conflict", "command_key identifies another operation")
            return {**ledger._visible(recorded, scopes, live=False), "idempotent": True}
        if state["revision"] != expected_revision or state["status"] != "active":
            raise IntakeError("revision_conflict", "iteration changed; inspect before accepting")
        proposal = state["data"].get("proposal")
        outcome = state["data"].get("outcome")
        if not proposal or not outcome:
            raise IntakeError("proposal_required", "record outcome and proposal first")
        target = state["inputs"]["playbook"]
        receipt = _bounded({"contract": CONTRACT, "session_id": session_id,
                            "session_revision": proposal["proposal_revision"], "playbook_id": target["id"],
                            "before_revision": target["version"],
                            "expected": state["inputs"]["expected"], "outcome": outcome,
                            "before_after_rationale": proposal["before_after_rationale"],
                            "proposal_sha256": _hash(proposal),
                            "proposal_recorded_at_ms": proposal["proposed_at_ms"]})
        content = proposal["content"]
        self.conn.execute("BEGIN")
        try:
            revised = IntakePlaybookStore(self.conn, now=self.now).revise(
                namespace, target["id"], "iteration:" + command_key,
                expected_revision=target["version"],
                title=content["title"], prerequisites=content["prerequisites"],
                environment=content["environment"],
                steps=[{key: step[key] for key in ("action", "expected_result", "recovery")}
                       for step in content["steps"]],
                verification=content["verification"],
                source_rationale=content["source_rationale"],
                iteration_receipt=receipt, principal_id=principal_id, scopes=scopes,
                _within_transaction=True,
            )
            accepted = {"id": target["id"], "revision": revised["revision"],
                        "proposal_sha256": receipt["proposal_sha256"]}
            ref = {"kind": "revised_artifact", "id": target["id"],
                   "namespace": namespace, "version": revised["revision"]}

            def apply(current: dict, _payload: dict) -> None:
                self._typed(current)
                if _hash(current["data"].get("proposal")) != receipt["proposal_sha256"]:
                    raise IntakeError("revision_conflict", "proposal changed while accepting; inspect")
                current["data"].update({"accepted_revision": accepted,
                                        "proposal": {**proposal, "review_state": "accepted"}})
                current["data"].pop("iteration_command", None)

            result = ledger.command(
                namespace, session_id, command_key, expected_revision=expected_revision,
                action="record", payload={"data": {"iteration_command": accepted},
                                          "references": [ref]},
                principal_id=principal_id, scopes=scopes, record_hook=apply,
                _within_transaction=True,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def review_stability(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, stable: bool, observation: str,
        principal_id: str, scopes: set[str],
    ) -> dict:
        if type(stable) is not bool:
            raise IntakeError("invalid_stability", "stable must be a boolean")
        observation = _text(observation, "stability observation", limit=2000)

        def apply(state: dict, _payload: dict) -> None:
            self._typed(state)
            if not state["data"].get("accepted_revision"):
                raise IntakeError("revision_required", "accept a playbook revision first")
            state["data"]["stability_review"] = {
                "criteria": state["inputs"]["stability_criteria"],
                "stable": stable, "observation": observation,
                "basis": "caller_reported", "at_ms": self.now(),
            }
            state["data"].pop("iteration_command", None)

        return IntakeStore(self.conn, now=self.now).command(
            namespace, session_id, command_key, expected_revision=expected_revision,
            action="record", payload={"data": {"iteration_command": {
                "stable": stable, "observation": observation}}},
            principal_id=principal_id, scopes=scopes, record_hook=apply,
        )

    @staticmethod
    def _typed(state: dict) -> None:
        if state["mode"] != "Iteration" or state["inputs"].get("iteration_contract") != CONTRACT:
            raise IntakeError("invalid_mode", "use a typed playbook Iteration session")
