"""Typed troubleshooting trail over the durable intake session ledger."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from src.kb.intake_modes import (
    IntakeError,
    IntakeStore,
    _bounded,
    _hash,
    _reference,
    _text,
)

CONTRACT = "noesis-problem-trail-v1"
STEP_KINDS = {"hypothesis", "proposal", "reported_attempt", "verification"}
ACTION_CONTRACT = "noesis-problem-action-v1"
ACTION_PARAMETER_FIELDS = {
    "service.restart": {"service", "reason"},
    "ticket.create": {"project", "title", "description"},
}
ACTION_ADAPTER_ALLOWLIST = frozenset(ACTION_PARAMETER_FIELDS)
_ACTION_DDL = """
CREATE TABLE IF NOT EXISTS problem_action_executions(
 namespace TEXT NOT NULL,
 owner TEXT NOT NULL,
 idempotency_key TEXT NOT NULL,
 correlation_id TEXT NOT NULL,
 session_id TEXT NOT NULL,
 proposal_hash TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 consented_revision BIGINT NOT NULL,
 reserved_revision BIGINT NOT NULL,
 status TEXT NOT NULL,
 receipt_json TEXT,
 created_at_ms BIGINT NOT NULL,
 updated_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, owner, idempotency_key),
 UNIQUE(namespace, owner, correlation_id)
)
"""


class IntakeProblemStore:
    def __init__(
        self,
        conn: Any,
        *,
        now=None,
        adapters: Mapping[str, Callable[..., Any]] | None = None,
        initialize: bool = True,
    ):
        self.conn = conn
        self.sessions = IntakeStore(conn, now=now, initialize=initialize)
        self.now = self.sessions.now
        self.adapters = dict(adapters or {})
        if set(self.adapters) - ACTION_ADAPTER_ALLOWLIST or any(
            not callable(adapter) for adapter in self.adapters.values()
        ):
            raise IntakeError(
                "invalid_adapter",
                "only callable adapters for allowlisted Problem actions may be configured",
            )
        if initialize:
            conn.execute(_ACTION_DDL)

    @staticmethod
    def _action_owner(state: dict, principal_id: str, scopes: set[str], *, write: bool) -> None:
        operation = "write" if write else "read"
        if (
            state.get("owner") != principal_id
            or f"knowledge:intake:{operation}" not in scopes
            or f"namespace:{state.get('namespace')}:{operation}" not in scopes
        ):
            raise IntakeError(
                "unauthorized",
                "the problem owner and matching intake and namespace scopes are required",
            )

    @staticmethod
    def _problem_state(state: dict) -> None:
        if (
            state.get("mode") != "Problem-Solving"
            or state.get("inputs", {}).get("problem_contract") != CONTRACT
        ):
            raise IntakeError("invalid_mode", "session is not a typed problem trail")

    def _verification_evidence(
        self, raw_references: list[dict], namespace: str, principal_id: str,
        scopes: set[str],
    ) -> list[dict]:
        """Require evidence that is both accessible and current at verification time."""
        if not isinstance(raw_references, list) or not raw_references:
            raise IntakeError(
                "verification_evidence_required",
                "a passing verification must cite current accessible evidence",
            )
        refs = []
        for raw in raw_references:
            ref = _reference(raw, namespace, scopes)
            if ref["namespace"] != namespace:
                raise IntakeError(
                    "invalid_verification_evidence",
                    "verification evidence must be in the problem namespace",
                )
            if ref["kind"] == "exploration_source":
                row = self.conn.execute(
                    "SELECT s.version,s.content FROM intake_exploration_sources s "
                    "JOIN intake_exploration_source_revisions r "
                    "ON r.namespace=s.namespace AND r.owner=s.owner "
                    "AND r.source_id=s.source_id AND r.version=s.version "
                    "WHERE s.namespace=? AND s.owner=? AND s.source_id=?",
                    [namespace, principal_id, ref["id"]],
                ).fetchone()
                if not row or int(row[0]) != ref["version"] or not row[1].strip():
                    raise IntakeError(
                        "invalid_verification_evidence",
                        "Exploration evidence must be a current readable source snapshot",
                    )
            elif ref["kind"] == "intake_feed_item":
                from src.kb.intake_inbox import IntakeInboxStore

                try:
                    item = IntakeInboxStore(self.conn, initialize=False).inspect(
                        namespace, ref["id"], principal_id=principal_id, scopes=scopes,
                    )
                except IntakeError as exc:
                    raise IntakeError(
                        "invalid_verification_evidence",
                        "feed evidence is unavailable to the current owner",
                    ) from exc
                if item["source_version"] != ref["version"] or not item["content"].strip():
                    raise IntakeError(
                        "invalid_verification_evidence",
                        "feed evidence must be a current readable source snapshot",
                    )
            elif ref["kind"] in {"document", "document_revision"}:
                document_id = ref["id"]
                if ref["kind"] == "document_revision":
                    record = self.conn.execute(
                        "SELECT document_id,revision,payload_json,lifecycle "
                        "FROM document_revision_records WHERE revision_id=? "
                        "AND committed_watermark IS NOT NULL",
                        [ref["id"]],
                    ).fetchone()
                    if record:
                        document_id = record[0]
                else:
                    record = self.conn.execute(
                        "SELECT document_id,revision,payload_json,lifecycle "
                        "FROM document_revision_records WHERE document_id=? AND revision=? "
                        "AND committed_watermark IS NOT NULL",
                        [ref["id"], ref["version"]],
                    ).fetchone()
                if (
                    "operator" not in scopes
                    and f"document:{document_id}:read" not in scopes
                ):
                    raise IntakeError(
                        "unauthorized", "current document read scope is required for verification evidence",
                    )
                if (
                    not record or int(record[1]) != ref["version"]
                    or record[3] != "active"
                ):
                    raise IntakeError(
                        "invalid_verification_evidence",
                        "document evidence must be a current active retained revision",
                    )
                current = self.conn.execute(
                    "SELECT revision_id,revision,lifecycle FROM document_current_revisions "
                    "WHERE document_id=?",
                    [document_id],
                ).fetchone()
                if (
                    not current or int(current[1]) != ref["version"]
                    or current[2] != "active"
                ):
                    raise IntakeError(
                        "stale_verification_evidence",
                        "document evidence revision is no longer current",
                    )
                if ref["kind"] == "document_revision" and current[0] != ref["id"]:
                    raise IntakeError(
                        "stale_verification_evidence",
                        "document revision evidence is no longer current",
                    )
                try:
                    payload = json.loads(record[2])
                except (TypeError, ValueError) as exc:
                    raise IntakeError(
                        "invalid_verification_evidence", "document evidence payload is unavailable",
                    ) from exc
                if payload.get("_payload_reclaimed") or not isinstance(payload.get("content"), str) or not payload["content"].strip():
                    raise IntakeError(
                        "invalid_verification_evidence", "document evidence text is unavailable",
                    )
            else:
                raise IntakeError(
                    "invalid_verification_evidence",
                    "use a current Exploration source, feed item, or document revision",
                )
            refs.append(ref)
        return refs

    @staticmethod
    def _action_parameters(action: str, parameters: dict) -> dict:
        fields = ACTION_PARAMETER_FIELDS.get(action)
        if fields is None:
            raise IntakeError("invalid_action", "choose an allowlisted Problem action")
        if not isinstance(parameters, dict) or set(parameters) != fields:
            raise IntakeError("invalid_action", "action parameters must match the selected action")
        bounded = _bounded(parameters, limit=16_384)
        limits = {
            "service.restart": {"service": 200, "reason": 1000},
            "ticket.create": {"project": 200, "title": 500, "description": 5000},
        }[action]
        for field, limit in limits.items():
            _text(bounded.get(field), field, limit=limit)
        return {field: bounded[field].strip() for field in limits}

    def propose_action(
        self,
        namespace: str,
        session_id: str,
        command_key: str,
        *,
        expected_revision: int,
        action: str,
        parameters: dict,
        rationale: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Record an allowlisted action proposal without invoking an adapter."""
        parameters = self._action_parameters(action, parameters)
        rationale = _text(rationale, "action rationale", limit=3000)
        state = self.sessions._state(namespace, session_id)
        self._action_owner(state, principal_id, scopes, write=True)
        self._problem_state(state)
        proposal_body = {"action": action, "parameters": parameters, "rationale": rationale}
        proposal_hash = _hash(proposal_body)

        def record(current: dict, _payload: dict) -> None:
            self._problem_state(current)
            prior = current["data"].get("problem_action_proposal")
            if prior:
                history = current["data"].setdefault("problem_action_history", [])
                history.append({"proposal": prior, "state": "superseded"})
            prior_execution = current["data"].get("problem_action_execution")
            if prior_execution:
                history = current["data"].setdefault("problem_action_history", [])
                history.append({"execution": prior_execution, "state": prior_execution.get("status")})
            history = current["data"].get("problem_action_history", [])
            if len(history) > 100:
                del history[:-100]
            current["data"].pop("_problem_action_proposal", None)
            proposal = {
                **proposal_body,
                "contract": ACTION_CONTRACT,
                "proposal_id": "problem-action:" + _hash([
                    current["session_id"], current["revision"] + 1, proposal_hash
                ])[:24],
                "proposal_hash": proposal_hash,
                "proposal_revision": current["revision"] + 1,
                "proposed_by": principal_id,
                "proposed_at_ms": self.now(),
            }
            current["data"]["problem_action_proposal"] = proposal
            current["data"].pop("problem_action_consent", None)
            current["data"].pop("problem_action_execution", None)

        return self.sessions.command(
            namespace,
            session_id,
            command_key,
            expected_revision=expected_revision,
            action="record",
            payload={"data": {"_problem_action_proposal": proposal_body}},
            principal_id=principal_id,
            scopes=scopes,
            record_hook=record,
        )

    def preview_action(
        self, namespace: str, session_id: str, *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        """Preview the current proposal and adapter availability without execution."""
        state = self.sessions._state(namespace, session_id)
        self._action_owner(state, principal_id, scopes, write=False)
        self._problem_state(state)
        proposal = state["data"].get("problem_action_proposal")
        consent = state["data"].get("problem_action_consent")
        if not proposal:
            status = "proposal_required"
        elif proposal["action"] not in self.adapters:
            status = "adapter_unavailable"
        elif not consent or (
            consent.get("proposal_hash") != proposal.get("proposal_hash")
            or consent.get("consented_revision") != state["revision"]
        ):
            status = "consent_required"
        else:
            status = "ready"
        return {
            "contract": ACTION_CONTRACT,
            "status": status,
            "session_id": session_id,
            "session_revision": state["revision"],
            "proposal": proposal,
            "consent": consent,
            "adapter_configured": bool(proposal and proposal["action"] in self.adapters),
            "will_execute": False,
        }

    def consent_action(
        self,
        namespace: str,
        session_id: str,
        command_key: str,
        *,
        expected_revision: int,
        proposal_id: str,
        consent: bool,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Bind explicit owner consent to the exact current proposal and revision."""
        if consent is not True:
            raise IntakeError("consent_required", "explicit consent=true is required")
        proposal_id = _text(proposal_id, "proposal_id", limit=256)

        def record(state: dict, _payload: dict) -> None:
            self._problem_state(state)
            proposal = state["data"].get("problem_action_proposal")
            if not proposal or proposal.get("proposal_id") != proposal_id:
                raise IntakeError("proposal_changed", "inspect the current action proposal before consent")
            state["data"].pop("_problem_action_consent", None)
            state["data"]["problem_action_consent"] = {
                "proposal_id": proposal_id,
                "proposal_hash": proposal["proposal_hash"],
                "consented_revision": state["revision"] + 1,
                "consented_by": principal_id,
                "consented_at_ms": self.now(),
            }

        result = self.sessions.command(
            namespace,
            session_id,
            command_key,
            expected_revision=expected_revision,
            action="record",
            payload={"data": {"_problem_action_consent": proposal_id}},
            principal_id=principal_id,
            scopes=scopes,
            record_hook=record,
        )
        self._action_owner(result, principal_id, scopes, write=True)
        return result

    def execute_action(
        self,
        namespace: str,
        session_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        correlation_id: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Execute one configured allowlisted adapter call and persist its receipt."""
        idempotency_key = _text(idempotency_key, "idempotency_key", limit=256)
        correlation_id = _text(correlation_id, "correlation_id", limit=256)
        if type(expected_revision) is not int or expected_revision < 1:
            raise IntakeError("invalid_revision", "expected_revision must be positive")
        ledger = self.sessions
        state = ledger._state(namespace, session_id)
        self._action_owner(state, principal_id, scopes, write=True)
        self._problem_state(state)

        # A replay is resolved from the durable receipt before the session's
        # post-execution revision check. It still requires current owner scopes.
        existing = self.conn.execute(
            "SELECT correlation_id,session_id,proposal_hash,request_hash,consented_revision,"
            "status,receipt_json FROM problem_action_executions "
            "WHERE namespace=? AND owner=? AND idempotency_key=?",
            [namespace, principal_id, idempotency_key],
        ).fetchone()
        if existing:
            correlation, stored_session, proposal_hash, request_hash, consented_revision, status, raw_receipt = existing
            if (stored_session != session_id or correlation != correlation_id
                    or expected_revision != int(consented_revision)):
                raise IntakeError("idempotency_conflict", "idempotency key identifies another action request")
            expected_hash = _hash([namespace, principal_id, session_id, proposal_hash,
                                   int(consented_revision), correlation_id])
            if expected_hash != request_hash:
                raise IntakeError("idempotency_conflict", "stored action receipt does not match this request")
            return {
                "contract": ACTION_CONTRACT,
                "status": status,
                "receipt": json.loads(raw_receipt) if raw_receipt else None,
                "idempotent": True,
            }

        proposal = state["data"].get("problem_action_proposal")
        consent = state["data"].get("problem_action_consent")
        if (
            type(expected_revision) is not int
            or state["revision"] != expected_revision
            or not proposal
            or not consent
            or consent.get("proposal_id") != proposal.get("proposal_id")
            or consent.get("proposal_hash") != proposal.get("proposal_hash")
            or consent.get("consented_revision") != expected_revision
        ):
            raise IntakeError("consent_stale", "current explicit consent for this exact session revision is required")
        adapter = self.adapters.get(proposal["action"])
        if adapter is None:
            return {
                "contract": ACTION_CONTRACT,
                "status": "unavailable",
                "reason": "adapter_unavailable",
                "executed": False,
                "session_id": session_id,
                "session_revision": expected_revision,
                "proposal_id": proposal["proposal_id"],
                "idempotency_key": idempotency_key,
                "correlation_id": correlation_id,
            }

        proposal_hash = proposal["proposal_hash"]
        request_hash = _hash([namespace, principal_id, session_id, proposal_hash,
                              expected_revision, correlation_id])
        reservation_key = "problem-action:reserve:" + _hash(idempotency_key)[:32]
        self.conn.execute("BEGIN")
        try:
            current = ledger._state(namespace, session_id)
            self._action_owner(current, principal_id, scopes, write=True)
            self._problem_state(current)
            if current["revision"] != expected_revision:
                raise IntakeError("revision_conflict", "session changed; preview and consent again")
            current_proposal = current["data"].get("problem_action_proposal")
            current_consent = current["data"].get("problem_action_consent")
            if (
                not current_proposal
                or current_proposal.get("proposal_hash") != proposal_hash
                or not current_consent
                or current_consent.get("consented_revision") != expected_revision
            ):
                raise IntakeError("consent_stale", "proposal or consent changed before action reservation")
            raced = self.conn.execute(
                "SELECT correlation_id,session_id,proposal_hash,request_hash,consented_revision,"
                "status,receipt_json FROM problem_action_executions "
                "WHERE namespace=? AND owner=? AND idempotency_key=?",
                [namespace, principal_id, idempotency_key],
            ).fetchone()
            if raced:
                self.conn.execute("ROLLBACK")
                return self.execute_action(
                    namespace, session_id, expected_revision=expected_revision,
                    idempotency_key=idempotency_key, correlation_id=correlation_id,
                    principal_id=principal_id, scopes=scopes,
                )

            def reserve(current_state: dict, _payload: dict) -> None:
                self._problem_state(current_state)
                current_state["data"].pop("_problem_action_reservation", None)
                current_state["data"]["problem_action_execution"] = {
                    "status": "reserved",
                    "idempotency_key": idempotency_key,
                    "correlation_id": correlation_id,
                    "proposal_hash": proposal_hash,
                    "consented_revision": expected_revision,
                    "reserved_at_ms": self.now(),
                }

            reserved = ledger.command(
                namespace,
                session_id,
                reservation_key,
                expected_revision=expected_revision,
                action="record",
                payload={"data": {"_problem_action_reservation": request_hash}},
                principal_id=principal_id,
                scopes=scopes,
                record_hook=reserve,
                _within_transaction=True,
            )
            now_ms = self.now()
            self.conn.execute(
                "INSERT INTO problem_action_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, principal_id, idempotency_key, correlation_id, session_id,
                 proposal_hash, request_hash, expected_revision, reserved["revision"],
                 "reserved", None, now_ms, now_ms],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        # The durable reservation prevents a retry from invoking the adapter
        # again if this process exits during the external call.
        try:
            adapter_value = adapter(
                dict(proposal["parameters"]),
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
            result = _bounded(adapter_value, limit=64_000)
            if not isinstance(result, dict):
                raise IntakeError("invalid_adapter_result", "adapter must return a JSON object")
            status = "completed"
            error_code = None
        except Exception as exc:  # adapter outcome may be uncertain after invocation
            result = None
            status = "indeterminate"
            error_code = getattr(exc, "code", "adapter_call_uncertain")

        receipt = {
            "contract": ACTION_CONTRACT,
            "status": status,
            "session_id": session_id,
            "consented_revision": expected_revision,
            "reserved_revision": reserved["revision"],
            "proposal_id": proposal["proposal_id"],
            "proposal_hash": proposal_hash,
            "action": proposal["action"],
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
            "plugin_links": list(state.get("plugin_links", [])),
            "adapter_result": result,
            "error_code": error_code,
            "recorded_at_ms": self.now(),
        }
        receipt_json = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
        finalize_key = "problem-action:receipt:" + _hash(idempotency_key)[:32]
        self.conn.execute("BEGIN")
        try:
            def finalize(current_state: dict, _payload: dict) -> None:
                self._problem_state(current_state)
                current_state["data"].pop("_problem_action_receipt", None)
                current_execution = current_state["data"].get("problem_action_execution")
                if (
                    not current_execution
                    or current_execution.get("idempotency_key") != idempotency_key
                    or current_execution.get("proposal_hash") != proposal_hash
                ):
                    raise IntakeError("execution_conflict", "reserved action changed before receipt persistence")
                current_state["data"]["problem_action_execution"] = receipt
                trail = current_state["data"].setdefault("problem_trail", [])
                trail.append({
                    "kind": "action_execution",
                    "summary": f"Configured adapter action {proposal['action']}",
                    "observation": status,
                    "passed": status == "completed",
                    "at_ms": receipt["recorded_at_ms"],
                    "references": [],
                    "receipt_key": idempotency_key,
                    "correlation_id": correlation_id,
                })
                if len(trail) > 500:
                    raise IntakeError("trail_full", "problem trail step limit reached")

            finalized = ledger.command(
                namespace,
                session_id,
                finalize_key,
                expected_revision=reserved["revision"],
                action="record",
                payload={"data": {"_problem_action_receipt": _hash(receipt)}},
                principal_id=principal_id,
                scopes=scopes,
                record_hook=finalize,
                _within_transaction=True,
            )
            self.conn.execute(
                "UPDATE problem_action_executions SET status=?,receipt_json=?,updated_at_ms=? "
                "WHERE namespace=? AND owner=? AND idempotency_key=? AND status='reserved'",
                [status, receipt_json, self.now(), namespace, principal_id, idempotency_key],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            # The external call may already have happened. The durable reserve
            # remains indeterminate and is deliberately never dispatched again.
            return {
                "contract": ACTION_CONTRACT,
                "status": "indeterminate",
                "reason": "receipt_persistence_uncertain",
                "executed": True,
                "idempotency_key": idempotency_key,
                "correlation_id": correlation_id,
            }
        return {"contract": ACTION_CONTRACT, "status": status, "receipt": receipt,
                "session_revision": finalized["revision"], "idempotent": False}

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
        plugin_links: list[dict] | None = None,
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
            plugin_links=plugin_links,
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
            verified = kind == "verification" and passed is True
            if verified:
                refs = self._verification_evidence(
                    payload.get("references", []), namespace, state["owner"], scopes,
                )
                trail[-1]["references"] = refs
            if kind in {"reported_attempt", "verification"}:
                state["data"]["verified"] = verified
                state["data"]["verification"] = (
                    observation if verified else ""
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
