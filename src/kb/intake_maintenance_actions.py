"""Execute an authorized Maintenance checklist action against its finding.

``record_maintenance_finding`` only reports a disposition. This module runs a
supported action through the store that owns the target, bound to the impact
preview the caller reviewed:

* ``refresh`` of a failed ``maintenance_job`` → requeue it through
  :meth:`MaintenanceOrchestrator.retry` (operator or maintenance-admin scope).
* ``repair`` of a ``superseded_research_source`` finding → re-pin that project
  link to the source's current verified revision (a new project revision).
* ``archive`` of a ``research_project`` → an archived project revision; the
  prior revisions remain readable, so the change is recoverable history.
* ``delete`` of any finding target registered as a retention object → a
  retention GC plan and execution; holds, pins and retained dependents block
  it. Requires ``confirm_delete=True`` plus retention admin and execute scope.

The caller passes the ``preview_hash`` from ``preview_intake_maintenance_impact``;
a changed impact (new dependents, holds or revisions) rejects execution. The
action and the session receipt commit in one transaction and the command key
makes retries idempotent, so a retried call never repeats the side effect.
Unsupported target/action pairs fail explicitly instead of being reported done.
"""

from __future__ import annotations

from typing import Any

from src.kb.intake_maintenance import CONTRACT as MAINTENANCE_CONTRACT
from src.kb.intake_maintenance_impact import IntakeMaintenanceImpact
from src.kb.intake_modes import IntakeError, IntakeStore, _hash, _text

RECEIPT_CONTRACT = "noesis-intake-maintenance-action-v1"
SUPPORTED = {
    ("maintenance_job", "refresh"),
    ("research_project", "archive"),
    ("research_project", "repair"),
}
_WORKER_SCOPES = {"operator", "knowledge:maintenance:admin"}


def preview_hash(preview: dict) -> str:
    """Stable digest of an impact preview (the ``preview_hash`` field excluded)."""

    return _hash({key: value for key, value in preview.items() if key != "preview_hash"})


class IntakeMaintenanceActions:
    def __init__(self, conn: Any, *, now=None):
        self.conn = conn
        self.now = now

    def execute(
        self, namespace: str, session_id: str, command_key: str, *,
        expected_revision: int, finding_id: str, action: str,
        reviewed_preview_hash: str, confirm_delete: bool = False,
        principal_id: str, scopes: set[str],
    ) -> dict:
        finding_id = _text(finding_id, "finding_id", limit=80)
        preview_digest = _text(reviewed_preview_hash, "preview_hash", limit=128)
        if action not in {"refresh", "repair", "archive", "delete"}:
            raise IntakeError("invalid_action", "execute refresh, repair, archive, or delete")
        if type(confirm_delete) is not bool:
            raise IntakeError("invalid_input", "confirm_delete must be a boolean")
        ledger = IntakeStore(self.conn, now=self.now)
        payload = {"data": {"maintenance_action": {
            "finding_id": finding_id, "action": action,
            "preview_hash": preview_digest, "confirm_delete": confirm_delete,
        }}}
        replay = self.conn.execute(
            "SELECT 1 FROM intake_session_commands WHERE session_id=? AND command_key=?",
            [session_id, _text(command_key, "command_key", limit=256)],
        ).fetchone()
        if replay:
            # The ledger returns the stored receipt (or an idempotency conflict)
            # without touching the target again.
            return ledger.command(
                namespace, session_id, command_key, expected_revision=expected_revision,
                action="record", payload=payload, principal_id=principal_id, scopes=scopes,
            )
        state = ledger.inspect(namespace, session_id, principal_id=principal_id, scopes=scopes)
        if state["mode"] != "Maintenance" or state["inputs"].get("maintenance_contract") != MAINTENANCE_CONTRACT:
            raise IntakeError("invalid_mode", "use a typed Maintenance review session")
        snapshot = {item["id"]: item for item in state["inputs"]["findings"]}
        if finding_id not in snapshot:
            raise IntakeError("finding_not_found", "finding is not in this review snapshot")
        preview = IntakeMaintenanceImpact(self.conn, now=self.now).preview(
            namespace, finding_id, "refresh" if action == "repair" else action,
            principal_id=principal_id, scopes=scopes,
        )
        if preview_hash(preview) != preview_digest:
            raise IntakeError("impact_changed", "impact changed since preview; preview again before executing")
        target = preview["target"]
        if action == "delete":
            execute = self._delete_plan(namespace, preview, confirm_delete, principal_id, scopes)
        elif (target["kind"], action) in SUPPORTED:
            execute = None
        else:
            raise IntakeError(
                "action_unsupported",
                f"{action} is not executable for {target['kind']} findings; "
                "record a reported disposition instead",
            )

        self.conn.execute("BEGIN TRANSACTION")
        try:
            if execute is not None:
                outcome = execute()
            elif action == "repair":
                outcome = self._repair_source_pin(namespace, target, snapshot[finding_id], principal_id, scopes)
            else:
                outcome = self._apply(namespace, target, action, principal_id, scopes)
            receipt = {
                "contract": RECEIPT_CONTRACT, "finding_id": finding_id,
                "action": action, "target": target, "basis": "executed",
                "preview_hash": preview_digest, "outcome": outcome,
                "affected_at_preview": len(preview["affected"]),
            }

            def apply(session: dict, _payload: dict) -> None:
                if finding_id not in {item["id"] for item in session["inputs"]["findings"]}:
                    raise IntakeError("finding_not_found", "finding is not in this review snapshot")
                session["data"].setdefault("maintenance_reviews", {})[finding_id] = receipt
                session["data"].setdefault("maintenance_history", []).append(receipt)
                session["data"].setdefault("checklist", {})[finding_id] = "done"
                session["data"].pop("maintenance_action", None)

            result = ledger.command(
                namespace, session_id, command_key, expected_revision=expected_revision,
                action="record", payload=payload, principal_id=principal_id,
                scopes=scopes, record_hook=apply, _within_transaction=True,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**result, "maintenance_action": receipt}

    def _apply(self, namespace: str, target: dict, action: str, principal_id: str, scopes: set[str]) -> dict:
        if (target["kind"], action) == ("maintenance_job", "refresh"):
            if not scopes & _WORKER_SCOPES:
                raise IntakeError("unauthorized", "maintenance-admin scope is required to requeue a job")
            from src.kb.maintenance import MaintenanceError, MaintenanceOrchestrator

            try:
                job = MaintenanceOrchestrator(self.conn, now=self.now or _now, initialize=False).retry(
                    target["id"], principal_id=principal_id,
                )
            except MaintenanceError as exc:
                raise IntakeError(exc.code, str(exc)) from exc
            return {"job_id": target["id"], "status": job.get("status"), "operation": "retry"}
        from src.kb.research_projects import ResearchProjectError, ResearchProjectStore

        try:
            project = ResearchProjectStore(self.conn, initialize=False, now=self.now or _now).revise(
                namespace, target["id"], int(target["version"]), status="archived",
                principal_id=principal_id, scopes=scopes, _within_transaction=True,
            )
        except ResearchProjectError as exc:
            raise IntakeError(exc.code, str(exc)) from exc
        return {
            "project_id": project["project_id"], "status": project["status"],
            "revision": project["revision"], "prior_revision": int(target["version"]),
            "recoverable": "prior project revisions remain readable",
        }

    def _repair_source_pin(self, namespace: str, target: dict, finding: dict,
                           principal_id: str, scopes: set[str]) -> dict:
        """Re-pin a superseded intake source link to its current verified revision."""

        if finding.get("reason") != "superseded_research_source":
            raise IntakeError("action_unsupported", "repair applies to a superseded pinned research source")
        from src.kb.research_projects import ResearchProjectError, ResearchProjectStore

        projects = ResearchProjectStore(self.conn, initialize=False, now=self.now or _now)
        try:
            project = projects.inspect(namespace, target["id"], principal_id=principal_id, scopes=scopes)
        except ResearchProjectError as exc:
            raise IntakeError(exc.code, str(exc)) from exc
        if project["revision"] != int(target["version"]):
            raise IntakeError("impact_changed", "project changed since the finding; rescan first")
        links = [dict(link) for link in project["links"]]
        match = next(
            (link for link, status in zip(links, project["reference_availability"], strict=True)
             if link["kind"] == "intake_source" and status["status"] == "superseded"
             and f"source {link['id']} revision {link['revision']} " in finding.get("detail", "")),
            None,
        )
        if match is None:
            raise IntakeError("finding_resolved", "the superseded source link is no longer present")
        feed = match["id"].startswith("feed:")
        table, identity, version = (
            ("intake_inbox_items", "item_id", "source_version") if feed
            else ("intake_exploration_sources", "source_id", "version")
        )
        row = self.conn.execute(
            f"SELECT {version} FROM {table} WHERE namespace=? AND owner=? AND {identity}=?",
            [match.get("namespace", namespace), project["owner"], match["id"]],
        ).fetchone()
        if row is None:
            raise IntakeError("source_unavailable", "the current source revision is unavailable")
        previous = match["revision"]
        match["revision"] = int(row[0])
        try:
            revised = projects.revise(
                namespace, target["id"], project["revision"], replace_links=links,
                principal_id=principal_id, scopes=scopes, _within_transaction=True,
            )
        except ResearchProjectError as exc:
            raise IntakeError(exc.code, str(exc)) from exc
        return {
            "project_id": target["id"], "source_id": match["id"],
            "previous_revision": previous, "repinned_revision": match["revision"],
            "project_revision": revised["revision"],
            "recoverable": "the prior project revision keeps the original pin",
            "review_required": "re-check evidence cards and claims that cited the old revision",
        }

    def _delete_plan(self, namespace: str, preview: dict, confirm_delete: bool,
                     principal_id: str, scopes: set[str]):
        from src.kb.knowledge_retention import (
            ADMIN_SCOPE,
            EXECUTE_SCOPE,
            KnowledgeRetentionStore,
        )

        if not confirm_delete:
            raise IntakeError("confirmation_required", "deletion needs confirm_delete=true after reviewing the preview")
        if "operator" not in scopes and not {ADMIN_SCOPE, EXECUTE_SCOPE} <= scopes:
            raise IntakeError("unauthorized", "retention admin and execute scopes are required to delete")
        retention = preview["retention"]
        if retention.get("status") != "active":
            raise IntakeError("action_unsupported", "only an active retention object can be deleted through Maintenance")
        if retention.get("active_hold_ids"):
            raise IntakeError("deletion_blocked", "an active retention hold protects this object")
        dependents = [item for item in preview["affected"] if item["kind"] != "intake_session"]
        if dependents:
            raise IntakeError("deletion_blocked", "local dependents still reference this object")
        object_id = preview["impact_root"]["id"]
        store = KnowledgeRetentionStore(self.conn, initialize=False)
        retention_scopes = scopes | ({ADMIN_SCOPE, EXECUTE_SCOPE} if "operator" in scopes else set())
        plan = store.plan_gc(namespace, [object_id], principal_id=principal_id, scopes=retention_scopes)
        if object_id not in plan["eligible"]:
            raise IntakeError("deletion_blocked", "retention plan blocks deletion: " + ", ".join(
                plan["blocked"].get(object_id, {}).get("reason_codes", ["not_eligible"])))

        def run() -> dict:
            from src.kb.retention_coordination import _LOCK

            with _LOCK:
                job = store._execute_gc(namespace, plan, principal_id=principal_id, scopes=retention_scopes)
            return {"object_id": object_id, "retention_job_id": job["job_id"],
                    "status": job["status"], "tombstoned": job["tombstoned"],
                    "recoverable": "retention tombstone and audit keep provenance"}

        return run


def _now() -> int:
    import time

    return int(time.time() * 1000)


__all__ = ["IntakeMaintenanceActions", "RECEIPT_CONTRACT", "preview_hash"]
