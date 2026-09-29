"""Measured playbook iteration with accepted changes in playbook revision history."""

from __future__ import annotations

import time
from typing import Any

from src.kb.intake_modes import (
    IntakeError,
    IntakeStore,
    MODES,
    _bounded,
    _hash,
    _plugin_links,
    _reference,
    _text,
)
from src.kb.intake_playbooks import IntakePlaybookStore, _steps, _strings

CONTRACT = "noesis-intake-iteration-playbook-v1"
DECISION_CONTRACT = "noesis-intake-iteration-decision-v1"
REPORT_CONTRACT = "noesis-intake-iteration-report-v1"
CONCEPT_CONTRACT = "noesis-intake-iteration-concept-v1"
MODULO_NOTE_CONTRACT = "noesis-intake-iteration-modulo-note-v1"


def _concept_content(value: Any, concept_id: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"id", "name", "explanation", "card_ids"}:
        raise IntakeError("invalid_concept", "concept revision needs id, name, explanation, and cited card IDs")
    identity = _text(value["id"], "concept ID", limit=128)
    if identity != concept_id:
        raise IntakeError("invalid_concept", "Iteration cannot change the concept ID")
    card_ids = value["card_ids"]
    if not isinstance(card_ids, list) or not 1 <= len(card_ids) <= 100:
        raise IntakeError("invalid_concept", "concept revision needs 1–100 cited cards")
    normalized = {
        "id": identity,
        "name": _text(value["name"], "concept name", limit=256),
        "explanation": _text(value["explanation"], "concept explanation"),
        "card_ids": [_text(item, "card ID", limit=128) for item in card_ids],
    }
    if len(set(normalized["card_ids"])) != len(normalized["card_ids"]):
        raise IntakeError("invalid_concept", "concept card IDs must be unique")
    return normalized


def _modulo_note_content(value: Any, field: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise IntakeError("invalid_modulo_note", f"{field} must be a nonempty note object")
    result = _bounded(value, limit=32_000)
    for key in ("title", "body", "text", "content"):
        if key in result and not isinstance(result[key], str):
            raise IntakeError("invalid_modulo_note", f"note {key} must be text")
    if not any(isinstance(result.get(key), str) for key in ("title", "body", "text", "content")):
        raise IntakeError("invalid_modulo_note", "note must contain a title, body, text, or content field")
    return result


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

    def start_decision(
        self, namespace: str, request_key: str, *, decision_id: str,
        expected_revision: int, expected: str, stability_criteria: str,
        intent: str, principal_id: str, scopes: set[str],
        origin_session_id: str | None = None,
    ) -> dict:
        """Start a measured cycle pinned to an owned, versioned Decision Record."""
        from src.kb.decisions import DecisionError, DecisionStore

        if type(expected_revision) is not int or expected_revision < 1:
            raise IntakeError("invalid_revision", "expected_revision must be a positive integer")
        ledger = IntakeStore(self.conn, now=self.now)
        session_id = "intake:" + _hash([namespace, principal_id, request_key])[:32]
        try:
            ledger._state(namespace, session_id)
            existing = True
        except IntakeError as exc:
            if exc.code != "session_not_found":
                raise
            existing = False
        try:
            decision = DecisionStore(self.conn, now=self.now).inspect(
                namespace, decision_id, principal_id=principal_id, scopes=scopes,
            )
        except DecisionError as exc:
            raise IntakeError(exc.code, str(exc)) from exc
        if not existing and decision["revision"] != expected_revision:
            raise IntakeError("revision_conflict", "decision changed; inspect before iterating")
        target = {"kind": "decision", "id": decision_id,
                  "namespace": namespace, "version": expected_revision}
        return ledger.create(
            namespace, "Iteration", request_key, intent=intent,
            inputs={"iteration_contract": DECISION_CONTRACT, "decision": target,
                    "expected": _text(expected, "expected outcome", limit=2000),
                    "stability_criteria": _text(stability_criteria, "stability criteria", limit=2000)},
            references=[target],
            origin={"session_id": origin_session_id, "reason": "New feedback on decision"}
            if origin_session_id else None,
            principal_id=principal_id, scopes=scopes,
        )

    def start_report(
        self, namespace: str, request_key: str, *, report_id: str,
        expected_revision: int, expected: str, stability_criteria: str,
        intent: str, principal_id: str, scopes: set[str],
        origin_session_id: str | None = None,
    ) -> dict:
        """Start a measured cycle on an owned, versioned authored report."""
        from src.kb.authored_reports import AuthoredReportStore, ReportError

        if type(expected_revision) is not int or expected_revision < 1:
            raise IntakeError("invalid_revision", "expected_revision must be a positive integer")
        ledger = IntakeStore(self.conn, now=self.now)
        session_id = "intake:" + _hash([namespace, principal_id, request_key])[:32]
        try:
            ledger._state(namespace, session_id)
            existing = True
        except IntakeError as exc:
            if exc.code != "session_not_found":
                raise
            existing = False
        try:
            report = AuthoredReportStore(self.conn, now=self.now).inspect(
                namespace, report_id, principal_id=principal_id, scopes=scopes,
            )
        except ReportError as exc:
            raise IntakeError(exc.code, str(exc)) from exc
        if not existing and report["revision"] != expected_revision:
            raise IntakeError("revision_conflict", "report changed; inspect before iterating")
        target = {"kind": "report", "id": report_id,
                  "namespace": namespace, "version": expected_revision}
        return ledger.create(
            namespace, "Iteration", request_key, intent=intent,
            inputs={"iteration_contract": REPORT_CONTRACT, "report": target,
                    "expected": _text(expected, "expected outcome", limit=2000),
                    "stability_criteria": _text(stability_criteria, "stability criteria", limit=2000)},
            references=[target],
            origin={"session_id": origin_session_id, "reason": "New feedback on report"}
            if origin_session_id else None,
            principal_id=principal_id, scopes=scopes,
        )

    def start_concept(
        self, namespace: str, request_key: str, *, bundle_id: str,
        concept_id: str, expected_revision: int, expected: str,
        stability_criteria: str, intent: str, principal_id: str,
        scopes: set[str], origin_session_id: str | None = None,
    ) -> dict:
        """Start a cycle pinned to one concept inside a versioned Research Bundle."""
        from src.kb.intake_research_bundle import IntakeResearchBundleStore

        if type(expected_revision) is not int or expected_revision < 1:
            raise IntakeError("invalid_revision", "expected_revision must be positive")
        concept_id = _text(concept_id, "concept ID", limit=128)
        ledger = IntakeStore(self.conn, now=self.now)
        session_id = "intake:" + _hash([namespace, principal_id, request_key])[:32]
        try:
            ledger._state(namespace, session_id)
            existing = True
        except IntakeError as exc:
            if exc.code != "session_not_found":
                raise
            existing = False
        bundles = IntakeResearchBundleStore(self.conn, initialize=False)
        try:
            bundle = bundles.inspect(
                namespace, bundle_id, principal_id=principal_id, scopes=scopes,
                revision=expected_revision,
            )
        except IntakeError as exc:
            raise IntakeError(exc.code, str(exc)) from exc
        concept = next((item for item in bundle["document"]["concepts"]
                        if item["id"] == concept_id), None)
        if concept is None:
            raise IntakeError("concept_unavailable", "concept is not present in the pinned bundle revision")
        if not existing:
            current = bundles.inspect(
                namespace, bundle_id, principal_id=principal_id, scopes=scopes,
            )
            if current["revision"] != expected_revision:
                raise IntakeError("revision_conflict", "Research Bundle changed; inspect before iterating")
        target = {"kind": "concept", "id": concept_id, "bundle_id": bundle_id,
                  "namespace": namespace, "version": expected_revision}
        bundle_ref = {"kind": "research_bundle", "id": bundle_id,
                      "namespace": namespace, "version": expected_revision,
                      "locator": {"section": f"concepts/{concept_id}"}}
        return ledger.create(
            namespace, "Iteration", request_key, intent=intent,
            inputs={"iteration_contract": CONCEPT_CONTRACT, "concept": target,
                    "expected": _text(expected, "expected outcome", limit=2000),
                    "stability_criteria": _text(stability_criteria, "stability criteria", limit=2000)},
            references=[bundle_ref],
            origin={"session_id": origin_session_id, "reason": "New feedback on concept"}
            if origin_session_id else None,
            principal_id=principal_id, scopes=scopes,
        )

    def start_modulo_note(
        self, namespace: str, request_key: str, *, plugin_link: dict,
        source_snapshot: dict, expected: str, stability_criteria: str,
        intent: str, principal_id: str, scopes: set[str],
        origin_session_id: str | None = None,
    ) -> dict:
        """Pin a Modulo note snapshot for a local candidate Iteration cycle."""
        links = _plugin_links([plugin_link], namespace, scopes)
        link = links[0]
        if link["plugin_id"] != "notes-editor" or link["authority"] != "modulo":
            raise IntakeError("invalid_modulo_note", "target must be a Modulo-owned notes-editor record")
        snapshot = _modulo_note_content(source_snapshot, "source snapshot")
        target = {
            "kind": "modulo_note", "id": link["record_id"],
            "namespace": namespace, "version": link["authoritative_version"],
            "plugin_link": link, "source_snapshot_sha256": _hash(snapshot),
        }
        return IntakeStore(self.conn, now=self.now).create(
            namespace, "Iteration", request_key, intent=intent,
            inputs={"iteration_contract": MODULO_NOTE_CONTRACT,
                    "modulo_note": target, "source_snapshot": snapshot,
                    "expected": _text(expected, "expected outcome", limit=2000),
                    "stability_criteria": _text(stability_criteria, "stability criteria", limit=2000)},
            plugin_links=[link],
            origin={"session_id": origin_session_id, "reason": "New feedback on Modulo note"}
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
            if state["inputs"].get("iteration_contract") != CONTRACT:
                raise IntakeError("invalid_target", "use the Decision Record proposal tool for this session")
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

    def propose_decision(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, content: dict, before_after_rationale: str,
        principal_id: str, scopes: set[str],
    ) -> dict:
        """Save a complete, validated decision revision for explicit review."""
        from src.kb.decisions import DecisionError, DecisionStore, _content

        ledger = IntakeStore(self.conn, now=self.now)
        state = ledger._state(namespace, session_id)
        ledger._authorize_full_read(state, principal_id, scopes)
        self._typed(state)
        target = state["inputs"].get("decision")
        if not isinstance(target, dict) or target.get("kind") != "decision":
            raise IntakeError("invalid_target", "use a Decision Record Iteration session")
        try:
            current = DecisionStore(self.conn, now=self.now).inspect(
                namespace, target["id"], principal_id=principal_id, scopes=scopes,
            )
            replay = self.conn.execute(
                "SELECT 1 FROM intake_session_commands WHERE session_id=? AND command_key=?",
                [session_id, command_key],
            ).fetchone()
            if current["revision"] != target["version"] and not replay:
                raise IntakeError("revision_conflict", "decision changed; inspect before proposing")
            reviewed_content = _content(content)
            candidate = {**current, "content": reviewed_content}
            DecisionStore(self.conn, initialize=False, now=self.now)._authorize(
                candidate, principal_id, scopes, write=True,
            )
        except DecisionError as exc:
            raise IntakeError(exc.code, str(exc)) from exc
        rationale = _text(before_after_rationale, "before/after rationale", limit=5000)
        proposal = _bounded({
            "content": reviewed_content,
            "before_after_rationale": rationale,
            "review_state": "proposed",
            "basis": "caller_authored",
        }, limit=4 * 1024 * 1024)

        def apply(current_state: dict, _payload: dict) -> None:
            self._typed(current_state)
            if not current_state["data"].get("outcome"):
                raise IntakeError("outcome_required", "record observed measurements before proposing a change")
            if current_state["data"].get("accepted_revision"):
                raise IntakeError("already_accepted", "start a new cycle for another revision")
            current_state["data"]["proposal"] = {
                **proposal, "proposed_at_ms": self.now(),
                "proposal_revision": current_state["revision"] + 1,
            }
            current_state["data"].pop("iteration_command", None)

        return ledger.command(
            namespace, session_id, command_key, expected_revision=expected_revision,
            action="record", payload={"data": {"iteration_command": proposal}},
            principal_id=principal_id, scopes=scopes, record_hook=apply,
        )

    def propose_report(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, content: dict, before_after_rationale: str,
        principal_id: str, scopes: set[str],
    ) -> dict:
        """Save a complete, validated authored-report revision for review."""
        from src.kb.authored_reports import AuthoredReportStore, ReportError, validate_content

        ledger = IntakeStore(self.conn, now=self.now)
        state = ledger._state(namespace, session_id)
        ledger._authorize_full_read(state, principal_id, scopes)
        self._typed(state)
        target = state["inputs"].get("report")
        if not isinstance(target, dict) or target.get("kind") != "report":
            raise IntakeError("invalid_target", "use an authored-report Iteration session")
        try:
            reports = AuthoredReportStore(self.conn, now=self.now)
            current = reports.inspect(
                namespace, target["id"], principal_id=principal_id, scopes=scopes,
            )
            replay = self.conn.execute(
                "SELECT 1 FROM intake_session_commands WHERE session_id=? AND command_key=?",
                [session_id, command_key],
            ).fetchone()
            if current["revision"] != target["version"] and not replay:
                raise IntakeError("revision_conflict", "report changed; inspect before proposing")
            reviewed_content = validate_content(content)
            reports._authorize({**current, "content": reviewed_content}, principal_id, scopes, write=True)
        except ReportError as exc:
            raise IntakeError(exc.code, str(exc)) from exc
        rationale = _text(before_after_rationale, "before/after rationale", limit=5000)
        proposal = _bounded({
            "content": reviewed_content,
            "before_after_rationale": rationale,
            "review_state": "proposed",
            "basis": "caller_authored",
        }, limit=16 * 1024 * 1024)

        def apply(current_state: dict, _payload: dict) -> None:
            self._typed(current_state)
            if not current_state["data"].get("outcome"):
                raise IntakeError("outcome_required", "record observed measurements before proposing")
            if current_state["data"].get("accepted_revision"):
                raise IntakeError("already_accepted", "start a new cycle for another revision")
            current_state["data"]["proposal"] = {
                **proposal, "proposed_at_ms": self.now(),
                "proposal_revision": current_state["revision"] + 1,
            }
            current_state["data"].pop("iteration_command", None)

        return ledger.command(
            namespace, session_id, command_key, expected_revision=expected_revision,
            action="record", payload={"data": {"iteration_command": proposal}},
            principal_id=principal_id, scopes=scopes, record_hook=apply,
        )

    def propose_concept(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, concept: dict, before_after_rationale: str,
        principal_id: str, scopes: set[str],
    ) -> dict:
        """Save a complete cited concept revision for explicit review."""
        from src.kb.intake_research_bundle import IntakeResearchBundleStore

        ledger = IntakeStore(self.conn, now=self.now)
        state = ledger._state(namespace, session_id)
        ledger._authorize_full_read(state, principal_id, scopes)
        self._typed(state)
        target = state["inputs"].get("concept")
        if not isinstance(target, dict) or target.get("kind") != "concept":
            raise IntakeError("invalid_target", "use a Research Bundle concept Iteration session")
        reviewed = _concept_content(concept, target["id"])
        bundles = IntakeResearchBundleStore(self.conn, initialize=False, now=self.now)
        try:
            pinned = bundles.inspect(
                namespace, target["bundle_id"], principal_id=principal_id,
                scopes=scopes, revision=target["version"],
            )
            current = bundles.inspect(
                namespace, target["bundle_id"], principal_id=principal_id, scopes=scopes,
            )
            project = bundles._project(
                namespace, pinned["project_id"], principal_id, scopes, write=True,
            )
            candidate_document = {**pinned["document"], "concepts": [
                reviewed if item["id"] == target["id"] else item
                for item in pinned["document"]["concepts"]
            ]}
            candidate_document, _ = bundles._validate(
                project, candidate_document, principal_id, scopes,
            )
        except IntakeError as exc:
            raise IntakeError(exc.code, str(exc)) from exc
        replay = self.conn.execute(
            "SELECT 1 FROM intake_session_commands WHERE session_id=? AND command_key=?",
            [session_id, command_key],
        ).fetchone()
        if current["revision"] != target["version"] and not replay:
            raise IntakeError("revision_conflict", "Research Bundle changed; inspect before proposing")
        rationale = _text(before_after_rationale, "before/after rationale", limit=5000)
        proposal = _bounded({
            "concept": reviewed, "before_after_rationale": rationale,
            "review_state": "proposed", "basis": "caller_authored",
        })

        def apply(current_state: dict, _payload: dict) -> None:
            self._typed(current_state)
            latest = bundles.inspect(
                namespace, target["bundle_id"], principal_id=principal_id,
                scopes=scopes,
            )
            if latest["revision"] != target["version"]:
                raise IntakeError("revision_conflict", "Research Bundle changed; inspect before proposing")
            if not any(item["id"] == target["id"] for item in latest["document"]["concepts"]):
                raise IntakeError("concept_unavailable", "target concept is no longer present")
            if current_state["inputs"].get("concept") != target:
                raise IntakeError("revision_conflict", "pinned concept changed; inspect")
            if not current_state["data"].get("outcome"):
                raise IntakeError("outcome_required", "record observed measurements before proposing")
            if current_state["data"].get("accepted_revision"):
                raise IntakeError("already_accepted", "start a new cycle for another revision")
            current_state["data"]["proposal"] = {
                **proposal, "proposed_at_ms": self.now(),
                "proposal_revision": current_state["revision"] + 1,
            }
            current_state["data"].pop("iteration_command", None)

        return ledger.command(
            namespace, session_id, command_key, expected_revision=expected_revision,
            action="record", payload={"data": {"iteration_command": proposal}},
            principal_id=principal_id, scopes=scopes, record_hook=apply,
        )

    def propose_modulo_note(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, content: dict, before_after_rationale: str,
        principal_id: str, scopes: set[str],
    ) -> dict:
        """Record a local note candidate; this never writes to Modulo."""
        ledger = IntakeStore(self.conn, now=self.now)
        state = ledger._state(namespace, session_id)
        ledger._authorize_full_read(state, principal_id, scopes)
        self._typed(state)
        target = state["inputs"].get("modulo_note")
        if not isinstance(target, dict) or target.get("kind") != "modulo_note":
            raise IntakeError("invalid_target", "use a Modulo note Iteration session")
        if (len(state.get("plugin_links", [])) != 1
                or state["plugin_links"][0] != target["plugin_link"]
                or _hash(state["inputs"].get("source_snapshot")) != target["source_snapshot_sha256"]):
            raise IntakeError("source_snapshot_conflict", "pinned Modulo note snapshot changed")
        reviewed = _modulo_note_content(content, "proposed note")
        rationale = _text(before_after_rationale, "before/after rationale", limit=5000)
        proposal = _bounded({
            "content": reviewed, "before_after_rationale": rationale,
            "review_state": "proposed", "basis": "caller_authored",
        }, limit=64_000)

        def apply(current_state: dict, _payload: dict) -> None:
            self._typed(current_state)
            if (
                current_state["inputs"].get("modulo_note") != target
                or current_state.get("plugin_links") != [target["plugin_link"]]
                or _hash(current_state["inputs"].get("source_snapshot"))
                != target["source_snapshot_sha256"]
            ):
                raise IntakeError("source_snapshot_conflict", "pinned Modulo note snapshot changed")
            if not current_state["data"].get("outcome"):
                raise IntakeError("outcome_required", "record observed measurements before proposing")
            if current_state["data"].get("accepted_revision"):
                raise IntakeError("already_accepted", "start a new cycle for another revision")
            current_state["data"]["proposal"] = {
                **proposal, "proposed_at_ms": self.now(),
                "proposal_revision": current_state["revision"] + 1,
            }
            current_state["data"].pop("iteration_command", None)

        return ledger.command(
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
            if state["inputs"].get("iteration_contract") == CONCEPT_CONTRACT:
                from src.kb.intake_research_bundle import IntakeResearchBundleStore

                target = state["inputs"]["concept"]
                IntakeResearchBundleStore(self.conn, initialize=False).inspect(
                    namespace, target["bundle_id"], principal_id=principal_id,
                    scopes=scopes,
                )
            if state["inputs"].get("iteration_contract") == MODULO_NOTE_CONTRACT:
                target = state["inputs"].get("modulo_note") or {}
                if (
                    len(state.get("plugin_links", [])) != 1
                    or state["plugin_links"][0] != target.get("plugin_link")
                    or _hash(state["inputs"].get("source_snapshot"))
                    != target.get("source_snapshot_sha256")
                ):
                    raise IntakeError("source_snapshot_conflict", "pinned Modulo note snapshot changed")
            return {**ledger._visible(recorded, scopes, live=False), "idempotent": True}
        if state["revision"] != expected_revision or state["status"] != "active":
            raise IntakeError("revision_conflict", "iteration changed; inspect before accepting")
        proposal = state["data"].get("proposal")
        outcome = state["data"].get("outcome")
        if not proposal or not outcome:
            raise IntakeError("proposal_required", "record outcome and proposal first")
        target = state["inputs"].get("decision")
        if isinstance(target, dict) and target.get("kind") == "decision":
            return self._accept_decision(
                ledger, state, namespace, session_id, command_key,
                expected_revision, proposal, outcome, target, principal_id, scopes,
            )
        target = state["inputs"].get("report")
        if isinstance(target, dict) and target.get("kind") == "report":
            return self._accept_report(
                ledger, state, namespace, session_id, command_key,
                expected_revision, proposal, outcome, target, principal_id, scopes,
            )
        target = state["inputs"].get("concept")
        if isinstance(target, dict) and target.get("kind") == "concept":
            return self._accept_concept(
                ledger, state, namespace, session_id, command_key,
                expected_revision, proposal, outcome, target, principal_id, scopes,
            )
        target = state["inputs"].get("modulo_note")
        if isinstance(target, dict) and target.get("kind") == "modulo_note":
            return self._accept_modulo_note(
                ledger, state, namespace, session_id, command_key,
                expected_revision, proposal, outcome, target, principal_id, scopes,
            )
        target = state["inputs"].get("playbook")
        if not isinstance(target, dict) or target.get("kind") != "playbook":
            raise IntakeError("invalid_target", "Iteration target is unsupported")
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

    def accept_decision(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, principal_id: str, scopes: set[str],
    ) -> dict:
        state = IntakeStore(self.conn, now=self.now)._state(namespace, session_id)
        target = state["inputs"].get("decision")
        if not isinstance(target, dict) or target.get("kind") != "decision":
            raise IntakeError("invalid_target", "use a Decision Record Iteration session")
        return self.accept(
            namespace, session_id, command_key, expected_revision=expected_revision,
            principal_id=principal_id, scopes=scopes,
        )

    def accept_report(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, principal_id: str, scopes: set[str],
    ) -> dict:
        state = IntakeStore(self.conn, now=self.now)._state(namespace, session_id)
        target = state["inputs"].get("report")
        if not isinstance(target, dict) or target.get("kind") != "report":
            raise IntakeError("invalid_target", "use an authored-report Iteration session")
        return self.accept(
            namespace, session_id, command_key, expected_revision=expected_revision,
            principal_id=principal_id, scopes=scopes,
        )

    def accept_concept(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, principal_id: str, scopes: set[str],
    ) -> dict:
        state = IntakeStore(self.conn, now=self.now)._state(namespace, session_id)
        if state["inputs"].get("iteration_contract") != CONCEPT_CONTRACT:
            raise IntakeError("invalid_target", "use a Research Bundle concept Iteration session")
        return self.accept(
            namespace, session_id, command_key, expected_revision=expected_revision,
            principal_id=principal_id, scopes=scopes,
        )

    def accept_modulo_note(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, principal_id: str, scopes: set[str],
    ) -> dict:
        state = IntakeStore(self.conn, now=self.now)._state(namespace, session_id)
        if state["inputs"].get("iteration_contract") != MODULO_NOTE_CONTRACT:
            raise IntakeError("invalid_target", "use a Modulo note Iteration session")
        return self.accept(
            namespace, session_id, command_key, expected_revision=expected_revision,
            principal_id=principal_id, scopes=scopes,
        )

    def _accept_report(
        self, ledger: IntakeStore, state: dict, namespace: str, session_id: str,
        command_key: str, expected_revision: int, proposal: dict, outcome: dict,
        target: dict, principal_id: str, scopes: set[str],
    ) -> dict:
        from src.kb.authored_reports import AuthoredReportStore

        receipt = _bounded({
            "contract": REPORT_CONTRACT,
            "session_id": session_id,
            "session_revision": proposal["proposal_revision"],
            "report_id": target["id"],
            "before_revision": target["version"],
            "expected": state["inputs"]["expected"],
            "outcome": outcome,
            "before_after_rationale": proposal["before_after_rationale"],
            "proposal_sha256": _hash(proposal),
            "proposal_recorded_at_ms": proposal["proposed_at_ms"],
        }, limit=16 * 1024 * 1024)
        self.conn.execute("BEGIN")
        try:
            revised = AuthoredReportStore(self.conn, now=self.now).revise(
                namespace, target["id"], target["version"], proposal["content"],
                principal_id=principal_id, scopes=scopes,
                iteration_receipt=receipt, _within_transaction=True,
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

    def _accept_decision(
        self, ledger: IntakeStore, state: dict, namespace: str, session_id: str,
        command_key: str, expected_revision: int, proposal: dict, outcome: dict,
        target: dict, principal_id: str, scopes: set[str],
    ) -> dict:
        from src.kb.decisions import DecisionStore

        receipt = _bounded({
            "contract": DECISION_CONTRACT,
            "session_id": session_id,
            "session_revision": proposal["proposal_revision"],
            "decision_id": target["id"],
            "before_revision": target["version"],
            "expected": state["inputs"]["expected"],
            "outcome": outcome,
            "before_after_rationale": proposal["before_after_rationale"],
            "proposal_sha256": _hash(proposal),
            "proposal_recorded_at_ms": proposal["proposed_at_ms"],
        }, limit=4 * 1024 * 1024)
        self.conn.execute("BEGIN")
        try:
            revised = DecisionStore(self.conn, now=self.now).revise(
                namespace, target["id"], target["version"], proposal["content"],
                principal_id=principal_id, scopes=scopes,
                iteration_receipt=receipt, _within_transaction=True,
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

    def _accept_concept(
        self, ledger: IntakeStore, state: dict, namespace: str, session_id: str,
        command_key: str, expected_revision: int, proposal: dict, outcome: dict,
        target: dict, principal_id: str, scopes: set[str],
    ) -> dict:
        from src.kb.intake_research_bundle import IntakeResearchBundleStore

        bundles = IntakeResearchBundleStore(self.conn, now=self.now)
        receipt = _bounded({
            "contract": CONCEPT_CONTRACT,
            "session_id": session_id,
            "session_revision": proposal["proposal_revision"],
            "bundle_id": target["bundle_id"],
            "concept_id": target["id"],
            "before_revision": target["version"],
            "expected": state["inputs"]["expected"],
            "outcome": outcome,
            "before_after_rationale": proposal["before_after_rationale"],
            "proposal_sha256": _hash(proposal),
            "proposal_recorded_at_ms": proposal["proposed_at_ms"],
        }, limit=64_000)
        self.conn.execute("BEGIN")
        try:
            current = bundles.inspect(
                namespace, target["bundle_id"], principal_id=principal_id,
                scopes=scopes,
            )
            if current["revision"] != target["version"]:
                raise IntakeError("revision_conflict", "Research Bundle changed; inspect before accepting")
            if not any(item["id"] == target["id"] for item in current["document"]["concepts"]):
                raise IntakeError("concept_unavailable", "target concept is no longer present")
            concept = proposal["concept"]
            document = {
                **current["document"],
                "concepts": [
                    concept if item["id"] == target["id"] else item
                    for item in current["document"]["concepts"]
                ],
            }
            project = bundles._project(
                namespace, current["project_id"], principal_id, scopes, write=True,
            )
            document, _ = bundles._validate(project, document, principal_id, scopes)
            revised = bundles.save(
                namespace, current["project_id"], "iteration:" + command_key,
                document, expected_revision=target["version"],
                iteration_receipt=receipt, principal_id=principal_id, scopes=scopes,
                _within_transaction=True,
            )
            accepted = {
                "id": target["bundle_id"], "bundle_id": target["bundle_id"],
                "concept_id": target["id"], "revision": revised["revision"],
                "proposal_sha256": receipt["proposal_sha256"],
            }
            ref = {
                "kind": "revised_artifact", "id": target["bundle_id"],
                "namespace": namespace, "version": revised["revision"],
            }

            def apply(current_state: dict, _payload: dict) -> None:
                self._typed(current_state)
                if _hash(current_state["data"].get("proposal")) != receipt["proposal_sha256"]:
                    raise IntakeError("revision_conflict", "proposal changed while accepting; inspect")
                if current_state["inputs"].get("concept") != target:
                    raise IntakeError("revision_conflict", "pinned concept changed while accepting")
                current_state["data"].update({
                    "accepted_revision": accepted,
                    "proposal": {**proposal, "review_state": "accepted"},
                })
                current_state["data"].pop("iteration_command", None)

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

    def _accept_modulo_note(
        self, ledger: IntakeStore, state: dict, namespace: str, session_id: str,
        command_key: str, expected_revision: int, proposal: dict, outcome: dict,
        target: dict, principal_id: str, scopes: set[str],
    ) -> dict:
        """Accept a local candidate while explicitly leaving Modulo untouched."""
        if (
            len(state.get("plugin_links", [])) != 1
            or state["plugin_links"][0] != target["plugin_link"]
            or _hash(state["inputs"].get("source_snapshot"))
            != target["source_snapshot_sha256"]
        ):
            raise IntakeError("source_snapshot_conflict", "pinned Modulo note snapshot changed")
        receipt = _bounded({
            "contract": MODULO_NOTE_CONTRACT,
            "session_id": session_id,
            "session_revision": proposal["proposal_revision"],
            "note_id": target["id"],
            "source_revision": target["version"],
            "source_snapshot_sha256": target["source_snapshot_sha256"],
            "expected": state["inputs"]["expected"],
            "outcome": outcome,
            "content": proposal["content"],
            "before_after_rationale": proposal["before_after_rationale"],
            "proposal_sha256": _hash(proposal),
            "proposal_recorded_at_ms": proposal["proposed_at_ms"],
            "plugin_link": target["plugin_link"],
            "local_only": True,
            "modulo_access_state": "not_checked_by_noesis",
            "writeback_state": "not_written",
        }, limit=128_000)
        accepted = {
            "id": target["id"], "revision": proposal["proposal_revision"],
            "session_revision": expected_revision + 1,
            "source_revision": target["version"],
            "source_snapshot_sha256": target["source_snapshot_sha256"],
            "content": proposal["content"],
            "proposal_sha256": receipt["proposal_sha256"],
            "command_key": command_key,
            "local_only": True,
            "modulo_access_state": "not_checked_by_noesis",
            "writeback_state": "not_written",
        }
        ref = {
            "kind": "modulo_note_candidate", "id": session_id,
            "namespace": namespace, "version": expected_revision + 1,
        }
        self.conn.execute("BEGIN")
        try:
            def apply(current_state: dict, _payload: dict) -> None:
                self._typed(current_state)
                if _hash(current_state["data"].get("proposal")) != receipt["proposal_sha256"]:
                    raise IntakeError("revision_conflict", "proposal changed while accepting; inspect")
                if (
                    current_state["inputs"].get("modulo_note") != target
                    or current_state.get("plugin_links") != [target["plugin_link"]]
                    or _hash(current_state["inputs"].get("source_snapshot"))
                    != target["source_snapshot_sha256"]
                ):
                    raise IntakeError("source_snapshot_conflict", "pinned Modulo note snapshot changed")
                history = current_state["data"].setdefault("modulo_note_candidate_history", [])
                if len(history) >= 500:
                    raise IntakeError("candidate_history_limit", "note candidate history exceeds 500 receipts")
                history.append(receipt)
                current_state["data"].update({
                    "accepted_revision": accepted,
                    "proposal": {**proposal, "review_state": "accepted"},
                })
                current_state["data"].pop("iteration_command", None)

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
                raise IntakeError("revision_required", "accept the proposed artifact revision first")
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

    def route_gap(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, target_mode: str, reason: str, intent: str,
        inputs: dict | None, references: list[dict] | None,
        principal_id: str, scopes: set[str],
    ) -> dict:
        """Atomically link an observed gap to a new, idempotent intake session."""
        if target_mode not in MODES or target_mode == "Iteration":
            raise IntakeError("invalid_mode", "choose a supported upstream mode")
        reason = _text(reason, "gap reason", limit=2000)
        command_key = _text(command_key, "command_key", limit=256)
        ledger = IntakeStore(self.conn, now=self.now)
        source = ledger._state(namespace, session_id)
        ledger._authorize_full_read(source, principal_id, scopes)
        self._typed(source)
        if not source["data"].get("outcome"):
            raise IntakeError("outcome_required", "record the observed outcome before routing a gap")
        child_key = "iteration-gap:" + _hash([session_id, command_key])[:32]
        self.conn.execute("BEGIN")
        try:
            child = ledger.create(
                namespace, target_mode, child_key, intent=intent,
                inputs=inputs or {}, references=references or [],
                origin={"session_id": session_id, "reason": reason},
                principal_id=principal_id, scopes=scopes, _within_transaction=True,
            )
            child_origin = ledger._state(namespace, child["session_id"], 1)
            handoff = {
                "session_id": child["session_id"], "mode": target_mode,
                "reason": reason, "source_session_id": session_id,
                "source_revision": expected_revision,
                "references": child_origin["references"],
                "at_ms": child_origin["created_at_ms"],
            }

            def apply(state: dict, _payload: dict) -> None:
                self._typed(state)
                if not state["data"].get("outcome"):
                    raise IntakeError("outcome_required", "record the observed outcome before routing a gap")
                state["data"].setdefault("gap_handoffs", []).append(handoff)
                state["data"].pop("iteration_command", None)

            parent = ledger.command(
                namespace, session_id, command_key,
                expected_revision=expected_revision, action="record",
                payload={"data": {"iteration_command": handoff}},
                principal_id=principal_id, scopes=scopes, record_hook=apply,
                _within_transaction=True,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"parent": parent, "child": child, "handoff": handoff,
                "idempotent": bool(parent.get("idempotent"))}

    @staticmethod
    def _typed(state: dict) -> None:
        if state["mode"] != "Iteration":
            raise IntakeError("invalid_mode", "use a typed Iteration session")
        contract = state["inputs"].get("iteration_contract")
        target_key = {
            CONTRACT: "playbook", DECISION_CONTRACT: "decision",
            REPORT_CONTRACT: "report", CONCEPT_CONTRACT: "concept",
            MODULO_NOTE_CONTRACT: "modulo_note",
        }.get(contract)
        target = state["inputs"].get(target_key) if target_key else None
        expected_kind = (
            "concept" if contract == CONCEPT_CONTRACT else
            "modulo_note" if contract == MODULO_NOTE_CONTRACT else target_key
        )
        if (not isinstance(target, dict) or target.get("kind") != expected_kind
                or not isinstance(target.get("id"), str) or not target["id"].strip()
                or target.get("namespace") != state.get("namespace")
                or type(target.get("version")) is not int or target["version"] < 1):
            raise IntakeError("invalid_mode", "use a supported typed Iteration session")
