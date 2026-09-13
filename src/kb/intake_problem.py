"""Typed troubleshooting trail over the durable intake session ledger."""

from __future__ import annotations

from typing import Any

from src.kb.intake_modes import IntakeError, IntakeStore, _reference, _text

CONTRACT = "noesis-problem-trail-v1"
STEP_KINDS = {"hypothesis", "proposal", "reported_attempt", "verification"}


class IntakeProblemStore:
    def __init__(self, conn: Any, *, now=None):
        self.sessions = IntakeStore(conn, now=now)

    def start(
        self,
        namespace: str,
        request_key: str,
        *,
        symptom: str,
        environment: str,
        urgency: str,
        success_check: str,
        origin: dict | None = None,
        workspace_links: list[dict] | None = None,
        references: list[dict] | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Open a problem without requiring a research project or evidence bundle."""
        inputs = {
            "problem_contract": CONTRACT,
            "symptom": _text(symptom, "symptom"),
            "environment": _text(environment, "environment/version context"),
            "urgency": _text(urgency, "urgency", limit=256),
            "success_check": _text(success_check, "observable success check"),
        }
        return self.sessions.create(
            namespace,
            "Problem-Solving",
            request_key,
            intent=inputs["symptom"],
            inputs=inputs,
            origin=origin,
            workspace_links=workspace_links,
            references=references,
            principal_id=principal_id,
            scopes=scopes,
        )

    def record_step(
        self,
        namespace: str,
        session_id: str,
        command_key: str,
        *,
        expected_revision: int,
        kind: str,
        summary: str,
        observation: str | None = None,
        next_action: str | None = None,
        passed: bool | None = None,
        references: list[dict] | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        if kind not in STEP_KINDS:
            raise IntakeError("invalid_step", "select a troubleshooting step kind")
        summary = _text(summary, "step summary")
        if kind in {"reported_attempt", "verification"}:
            observation = _text(observation, "observed result")
        elif observation is not None:
            raise IntakeError("invalid_step", "only attempts and checks have observations")
        if kind == "verification":
            if type(passed) is not bool:
                raise IntakeError("invalid_step", "verification needs a passed boolean")
        elif passed is not None:
            raise IntakeError("invalid_step", "only verification can be marked passed")
        if next_action is not None:
            next_action = _text(next_action, "next action")
        step = {
            "kind": kind,
            "summary": summary,
            "observation": observation,
            "next_action": next_action,
            "passed": passed,
        }

        def append(state: dict, payload: dict) -> None:
            if (
                state["mode"] != "Problem-Solving"
                or state["inputs"].get("problem_contract") != CONTRACT
            ):
                raise IntakeError("invalid_mode", "session is not a typed problem trail")
            state["data"].pop("_problem_step", None)
            trail = state["data"].setdefault("problem_trail", [])
            if len(trail) >= 500:
                raise IntakeError("trail_full", "problem trail step limit reached")
            trail.append(
                {
                    **step,
                    "at_ms": self.sessions.now(),
                    "references": [
                        _reference(ref, namespace, scopes)
                        for ref in payload.get("references", [])
                    ],
                }
            )
            if kind in {"reported_attempt", "verification"}:
                state["data"]["verified"] = kind == "verification" and passed is True
                state["data"]["verification"] = (
                    observation if state["data"]["verified"] else ""
                )

        return self.sessions.command(
            namespace,
            session_id,
            command_key,
            expected_revision=expected_revision,
            action="record",
            payload={"data": {"_problem_step": step}, "references": references or []},
            principal_id=principal_id,
            scopes=scopes,
            record_hook=append,
        )
