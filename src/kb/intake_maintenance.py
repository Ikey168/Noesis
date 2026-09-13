"""Bounded cross-mode review queue and typed, reported maintenance checklist.

Findings are read-only observations. A reported repair/refresh is never an
execution receipt, and this module performs no archive or delete mutation.
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.kb.authored_reports import AuthoredReportStore, ReportError
from src.kb.intake_creation import IntakeCreationStore
from src.kb.intake_modes import IntakeError, IntakeStore, _hash, _text
from src.kb.intake_playbooks import IntakePlaybookStore
from src.kb.intake_practice import IntakePracticeStore

CONTRACT = "noesis-intake-maintenance-review-v1"
MONTH_MS = 30 * 86_400_000
MAX_FINDINGS = 100
ACTIONS = {"defer", "reviewed", "refresh_reported", "repair_reported", "archive_candidate"}


def _has_table(conn: Any, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [name],
    ).fetchone())


class IntakeMaintenanceStore:
    def __init__(self, conn: Any, *, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))

    def scan(self, namespace: str, *, principal_id: str, scopes: set[str]) -> dict:
        """Return explainable current findings, never opaque scores or hidden content."""
        namespace = _text(namespace, "namespace", limit=128)
        IntakeStore._authorize(
            {"namespace": namespace, "owner": principal_id}, principal_id, scopes,
        )
        now_ms = self.now()
        findings: list[dict] = []

        def add(kind: str, identity: str, version: int, reason: str,
                suggested_action: str, detail: str, instance: str = "") -> None:
            findings.append({
                "id": "finding:" + _hash([namespace, kind, identity, version, reason, instance])[:32],
                "reason": reason, "detail": detail,
                "target": {"kind": kind, "id": identity, "namespace": namespace,
                           "version": version},
                "suggested_action": suggested_action,
            })
            if len(findings) > MAX_FINDINGS:
                raise IntakeError("maintenance_queue_too_large", "review one namespace or a narrower set of artifacts")

        if _has_table(self.conn, "intake_practice_progress"):
            rows = self.conn.execute(
                "SELECT p.pack_id,p.revision,g.card_id,g.due_at_ms "
                "FROM intake_practice_packs p JOIN intake_practice_progress g "
                "ON p.pack_id=g.pack_id WHERE p.namespace=? AND p.owner=? "
                "AND g.due_at_ms<? ORDER BY g.due_at_ms,p.pack_id,g.card_id LIMIT ?",
                [namespace, principal_id, now_ms, MAX_FINDINGS + 1],
            ).fetchall()
            practice = IntakePracticeStore(self.conn, initialize=False, now=self.now)
            for pack_id, revision, card_id, due_at in rows:
                try:
                    pack = practice.inspect_pack(namespace, pack_id,
                                                 principal_id=principal_id, scopes=scopes)
                except IntakeError as exc:
                    if exc.code != "unauthorized":
                        raise
                    add("practice_pack", pack_id, int(revision), "practice_source_access_unavailable",
                        "restore_access", "Referenced practice material cannot be read with current access",
                        card_id)
                    continue
                card = next((item for item in pack["cards"] if item["id"] == card_id), None)
                if card is None:
                    continue
                add("practice_pack", pack_id, int(revision), "overdue_practice",
                    "review_or_defer", f"{card['kind']} card {card_id} due at {int(due_at)}",
                    card_id)

        if _has_table(self.conn, "intake_playbooks"):
            rows = self.conn.execute(
                "SELECT playbook_id,revision,content_json FROM intake_playbooks "
                "WHERE namespace=? AND owner=? ORDER BY playbook_id",
                [namespace, principal_id],
            ).fetchall()
            playbooks = IntakePlaybookStore(self.conn, initialize=False, now=self.now)
            for playbook_id, revision, raw in rows:
                stored = json.loads(raw)
                last_edit = stored.get("updated_at_ms", stored.get("created_at_ms", now_ms))
                if stored.get("trust_state") != "draft" or now_ms - last_edit < MONTH_MS:
                    continue
                try:
                    playbooks.inspect(namespace, playbook_id,
                                      principal_id=principal_id, scopes=scopes)
                except IntakeError as exc:
                    if exc.code != "unauthorized":
                        raise
                    add("playbook", playbook_id, int(revision), "playbook_source_access_unavailable",
                        "restore_access", "A draft procedure has inaccessible source material")
                    continue
                add("playbook", playbook_id, int(revision), "old_draft_playbook",
                    "rehearse_or_archive", "Draft procedure has not been revised for at least 30 days")

        if _has_table(self.conn, "intake_playbook_runs"):
            rows = self.conn.execute(
                "SELECT run_id,revision,content_json FROM intake_playbook_runs "
                "WHERE namespace=? AND owner=? ORDER BY run_id",
                [namespace, principal_id],
            ).fetchall()
            playbooks = IntakePlaybookStore(self.conn, initialize=False, now=self.now)
            for run_id, revision, raw in rows:
                stored = json.loads(raw)
                observations = stored.get("observations", [])
                last_step_failed = bool(observations) and observations[-1].get("passed") is False
                verification = stored.get("verification")
                last_check_failed = isinstance(verification, dict) and verification.get("passed") is False
                if stored.get("status") not in {"active", "paused"} or not (
                    last_step_failed or last_check_failed
                ):
                    continue
                try:
                    playbooks.inspect_run(namespace, run_id,
                                          principal_id=principal_id, scopes=scopes)
                except IntakeError as exc:
                    if exc.code != "unauthorized":
                        raise
                    add("guided_playbook_run", run_id, int(revision),
                        "rehearsal_source_access_unavailable", "restore_access",
                        "A failed guided rehearsal references inaccessible source material")
                    continue
                stage = "final check" if last_check_failed else "step"
                add("guided_playbook_run", run_id, int(revision),
                    "failed_guided_rehearsal", "retry_or_revise",
                    f"The latest caller-reported {stage} failed; inspect the run before retrying")

        if _has_table(self.conn, "intake_creation_projects"):
            rows = self.conn.execute(
                "SELECT project_id,revision,content_json FROM intake_creation_projects "
                "WHERE namespace=? AND owner=? ORDER BY project_id",
                [namespace, principal_id],
            ).fetchall()
            creations = IntakeCreationStore(self.conn, initialize=False, now=self.now)
            for project_id, revision, raw in rows:
                stored = json.loads(raw)
                if stored.get("status") != "finished" or not stored.get("report"):
                    continue
                try:
                    creations._access(stored, principal_id, scopes)
                    current_report = AuthoredReportStore(self.conn, initialize=False).inspect(
                        namespace, stored["report"]["id"],
                        principal_id=principal_id, scopes=scopes,
                    )
                except (IntakeError, ReportError):
                    add("creation_project", project_id, int(revision),
                        "creation_report_access_unavailable", "restore_access",
                        "The accepted report cannot be checked with current access")
                    continue
                if current_report["revision"] != stored["report"]["revision"]:
                    add("creation_project", project_id, int(revision), "stale_created_report",
                        "review_new_revision", "The authored report changed after project acceptance")

        add("maintenance_review", "routine-health", 1, "routine_health_check",
            "review", "Review the configured system-health criteria")
        findings.sort(key=lambda item: (item["reason"], item["target"]["id"]))
        return {"contract": CONTRACT, "namespace": namespace, "owner": principal_id,
                "as_of_ms": now_ms, "findings": findings,
                "coverage": ["overdue_practice", "old_draft_playbook",
                             "failed_guided_rehearsal", "stale_created_report"],
                "limitations": ["Reported repair actions are not execution receipts",
                                "Source-pack failures and dependency impact are not yet composed"]}

    def start(
        self, namespace: str, request_key: str, *, intent: str,
        duration_minutes: int = 45, principal_id: str, scopes: set[str],
    ) -> dict:
        ledger = IntakeStore(self.conn, now=self.now)
        session_id = "intake:" + _hash([namespace, principal_id, request_key])[:32]
        try:
            existing = ledger._state(namespace, session_id)
        except IntakeError as exc:
            if exc.code != "session_not_found":
                raise
            existing = None
        queue = existing["inputs"]["findings"] if existing else self.scan(
            namespace, principal_id=principal_id, scopes=scopes,
        )["findings"]
        return ledger.create(
            namespace, "Maintenance", request_key, intent=intent,
            inputs={"maintenance_contract": CONTRACT, "findings": queue},
            duration_minutes=duration_minutes,
            references=[item["target"] for item in queue
                        if item["target"]["kind"] != "maintenance_review"],
            principal_id=principal_id, scopes=scopes,
        )

    def record_finding(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, finding_id: str, action: str,
        observation: str, principal_id: str, scopes: set[str],
    ) -> dict:
        finding_id = _text(finding_id, "finding_id", limit=80)
        if action not in ACTIONS:
            raise IntakeError("invalid_action", "select a supported review disposition")
        observation = _text(observation, "observation", limit=2000)

        def apply(state: dict, _payload: dict) -> None:
            if state["mode"] != "Maintenance" or state["inputs"].get("maintenance_contract") != CONTRACT:
                raise IntakeError("invalid_mode", "use a typed Maintenance review session")
            if finding_id not in {item["id"] for item in state["inputs"]["findings"]}:
                raise IntakeError("finding_not_found", "finding is not in this review snapshot")
            result = {"action": action, "observation": observation,
                      "at_ms": self.now(), "basis": "caller_reported"}
            state["data"].setdefault("maintenance_reviews", {})[finding_id] = result
            state["data"].setdefault("maintenance_history", []).append(
                {"finding_id": finding_id, **result}
            )
            state["data"].setdefault("checklist", {})[finding_id] = (
                "deferred" if action == "defer" else "done"
            )
            state["data"].pop("maintenance_command", None)

        return IntakeStore(self.conn, now=self.now).command(
            namespace, session_id, command_key, expected_revision=expected_revision,
            action="record", payload={"data": {"maintenance_command": {
                "finding_id": finding_id, "action": action,
                "observation": observation,
            }}}, principal_id=principal_id, scopes=scopes, record_hook=apply,
        )

    def assess_health(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, acceptable: bool, criteria: str,
        observation: str, principal_id: str, scopes: set[str],
    ) -> dict:
        if type(acceptable) is not bool:
            raise IntakeError("invalid_input", "acceptable must be a boolean")
        criteria = _text(criteria, "health criteria", limit=2000)
        observation = _text(observation, "health observation", limit=2000)

        def apply(state: dict, _payload: dict) -> None:
            if state["mode"] != "Maintenance" or state["inputs"].get("maintenance_contract") != CONTRACT:
                raise IntakeError("invalid_mode", "use a typed Maintenance review session")
            state["data"]["health_assessment"] = {
                "acceptable": acceptable, "criteria": criteria,
                "observation": observation, "at_ms": self.now(),
                "basis": "caller_reported",
            }
            state["data"]["health_acceptable"] = acceptable
            state["data"].pop("maintenance_command", None)

        return IntakeStore(self.conn, now=self.now).command(
            namespace, session_id, command_key, expected_revision=expected_revision,
            action="record", payload={"data": {"maintenance_command": {
                "acceptable": acceptable, "criteria": criteria,
                "observation": observation,
            }}}, principal_id=principal_id, scopes=scopes, record_hook=apply,
        )
