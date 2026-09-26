"""Bounded cross-mode review queue and typed, reported maintenance checklist.

Findings are read-only observations. A reported repair/refresh is never an
execution receipt, and this module performs no archive or delete mutation.
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.kb.authored_reports import AuthoredReportStore, ReportError
from src.kb.evidence_changes import EvidenceResolver
from src.kb.intake_creation import IntakeCreationStore
from src.kb.intake_modes import IntakeError, IntakeStore, _hash, _text
from src.kb.intake_playbooks import IntakePlaybookStore
from src.kb.intake_practice import IntakePracticeStore, practice_source_status
from src.kb.research_projects import ResearchProjectError, ResearchProjectStore

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
                suggested_action: str, detail: str, instance: str = "",
                metadata: dict | None = None) -> None:
            findings.append({
                "id": "finding:" + _hash([namespace, kind, identity, version, reason, instance])[:32],
                "reason": reason, "detail": detail,
                "target": {"kind": kind, "id": identity, "namespace": namespace,
                           "version": version},
                "suggested_action": suggested_action,
                **({"metadata": metadata} if metadata else {}),
            })
            if len(findings) > MAX_FINDINGS:
                raise IntakeError("maintenance_queue_too_large", "review one namespace or a narrower set of artifacts")

        practice_scan_limited = False
        if _has_table(self.conn, "intake_practice_packs"):
            rows = self.conn.execute(
                "SELECT pack_id,revision FROM intake_practice_packs "
                "WHERE namespace=? AND owner=? ORDER BY pack_id LIMIT 501",
                [namespace, principal_id],
            ).fetchall()
            practice_scan_limited = len(rows) > 500
            practice = IntakePracticeStore(self.conn, initialize=False, now=self.now)
            for pack_id, revision in rows[:500]:
                try:
                    pack = practice.inspect_pack(namespace, pack_id,
                                                 principal_id=principal_id, scopes=scopes)
                except IntakeError as exc:
                    if exc.code != "unauthorized":
                        raise
                    continue
                for card in pack["cards"]:
                    source_status = practice_source_status(self.conn, card, principal_id, scopes)
                    if source_status not in {"superseded", "unavailable"}:
                        continue
                    add("practice_pack", pack_id, int(revision),
                        "superseded_practice_source" if source_status == "superseded" else "unavailable_practice_source",
                        "revise_practice_pack" if source_status == "superseded" else "restore_or_replace_source",
                        f"Practice card {card['id']} cites an intake source that is {source_status}; review the answer before another attempt",
                        card["id"])

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
                source_status = practice_source_status(self.conn, card, principal_id, scopes)
                if source_status in {"superseded", "unavailable"}:
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

        report_scope_available = (
            "operator" in scopes or "knowledge:reports:read" in scopes
        )
        report_store_available = _has_table(self.conn, "authored_reports")
        report_scan_available = report_scope_available and report_store_available
        report_scan_limited = False
        citation_scope_limited = False
        report_records: list[dict[str, Any]] = []
        if report_scan_available and _has_table(self.conn, "authored_reports"):
            rows = self.conn.execute(
                "SELECT report_id,revision FROM authored_reports "
                "WHERE namespace=? AND owner=? ORDER BY report_id LIMIT 501",
                [namespace, principal_id],
            ).fetchall()
            report_scan_limited = len(rows) > 500
            reports = AuthoredReportStore(self.conn, initialize=False)
            resolver = EvidenceResolver(self.conn, scopes)
            for report_id, revision in rows[:500]:
                try:
                    report = reports.inspect(
                        namespace, report_id, principal_id=principal_id,
                        scopes=scopes,
                    )
                except ReportError as exc:
                    if exc.code != "unauthorized":
                        raise
                    citation_scope_limited = True
                    continue
                report_records.append({
                    "id": report_id,
                    "revision": int(revision),
                    "updated_at_ms": report.get("updated_at_ms"),
                    "content_sha256": _hash(report["content"]),
                })
                for section in report["content"]["sections"]:
                    for assertion in section["assertions"]:
                        for dependency in assertion["dependencies"]:
                            if dependency["kind"] != "source":
                                continue
                            comparison = resolver.compare(dependency)
                            if comparison["reason"].endswith("access_unavailable"):
                                citation_scope_limited = True
                                continue
                            if comparison["status"] != "affected":
                                continue
                            change_reason = comparison["reason"]
                            latest = comparison.get("after") or {}
                            current_revision = latest.get("revision_id")
                            pinned_revision = dependency["revision"]
                            if (
                                current_revision == pinned_revision
                                and change_reason not in {
                                    "confirmed_withdrawal",
                                    "provider_notice_requires_review",
                                }
                            ):
                                continue
                            add(
                                "authored_report", report_id, int(revision),
                                "outdated_citation", "review_citation",
                                f"Assertion {assertion['id']} cites source revision "
                                f"{pinned_revision}; current evidence reports "
                                f"{change_reason}",
                                f"{section['id']}:{assertion['id']}:{_hash(dependency)[:16]}",
                                metadata={
                                    "section_id": section["id"],
                                    "assertion_id": assertion["id"],
                                    "source_id": dependency["id"],
                                    "pinned_revision": pinned_revision,
                                    "current_revision": current_revision,
                                    "source_status": latest.get("lifecycle"),
                                    "comparison_reason": change_reason,
                                },
                            )

        report_reference_scan_limited = False
        report_reference_stores: list[str] = []
        linked_report_ids: set[str] = set()
        visible_report_ids = {item["id"] for item in report_records}

        if visible_report_ids and _has_table(self.conn, "intake_creation_projects"):
            report_reference_stores.append("intake_creation_projects")
            rows = self.conn.execute(
                "SELECT project_id,revision,content_json FROM intake_creation_projects "
                "WHERE namespace=? AND owner=? ORDER BY project_id LIMIT 501",
                [namespace, principal_id],
            ).fetchall()
            report_reference_scan_limited |= len(rows) > 500
            for project_id, _revision, raw in rows[:500]:
                project = json.loads(raw)
                try:
                    IntakeCreationStore._access(project, principal_id, scopes)
                except IntakeError as exc:
                    if exc.code != "unauthorized":
                        raise
                    report_reference_scan_limited = True
                    continue
                report_link = project.get("report")
                if isinstance(report_link, dict) and report_link.get("id") in visible_report_ids:
                    linked_report_ids.add(report_link["id"])

        if visible_report_ids and _has_table(self.conn, "intake_sessions"):
            report_reference_stores.append("intake_sessions")
            rows = self.conn.execute(
                "SELECT session_id,content_json FROM intake_sessions "
                "WHERE namespace=? AND owner=? ORDER BY session_id LIMIT 501",
                [namespace, principal_id],
            ).fetchall()
            report_reference_scan_limited |= len(rows) > 500
            for _session_id, raw in rows[:500]:
                session = json.loads(raw)
                try:
                    IntakeStore._authorize_full_read(session, principal_id, scopes)
                except IntakeError as exc:
                    if exc.code != "unauthorized":
                        raise
                    report_reference_scan_limited = True
                    continue
                for reference in session.get("references", []):
                    if (
                        isinstance(reference, dict)
                        and reference.get("kind") in {"report", "authored_report"}
                        and reference.get("id") in visible_report_ids
                    ):
                        linked_report_ids.add(reference["id"])

        def report_id_filter_query(table: str, select: str, *, extra: str = "") -> list[Any]:
            nonlocal report_reference_scan_limited
            if not visible_report_ids:
                return []
            placeholders = ",".join("?" for _ in visible_report_ids)
            rows = self.conn.execute(
                f"SELECT {select} FROM {table} WHERE namespace=? AND report_id IN ({placeholders}) {extra} LIMIT 501",
                [namespace, *sorted(visible_report_ids)],
            ).fetchall()
            report_reference_scan_limited |= len(rows) > 500
            return rows[:500]

        if visible_report_ids and _has_table(self.conn, "report_update_subscriptions"):
            report_reference_stores.append("report_update_subscriptions")
            linked_report_ids.update(
                row[0] for row in report_id_filter_query(
                    "report_update_subscriptions", "report_id",
                )
            )

        if visible_report_ids and _has_table(self.conn, "report_edit_proposals"):
            report_reference_stores.append("report_edit_proposals")
            linked_report_ids.update(
                row[0] for row in report_id_filter_query(
                    "report_edit_proposals", "report_id",
                )
            )

        subscription_scan_complete = True
        if visible_report_ids and _has_table(self.conn, "knowledge_subscriptions"):
            if "operator" in scopes or "knowledge:subscriptions:read" in scopes:
                report_reference_stores.append("knowledge_subscriptions")
                rows = self.conn.execute(
                    "SELECT subscription_id,query_json FROM knowledge_subscriptions "
                    "WHERE namespace=? AND owner_principal=? "
                    "ORDER BY subscription_id LIMIT 501",
                    [namespace, principal_id],
                ).fetchall()
                subscription_scan_complete = len(rows) <= 500
                for _subscription_id, raw in rows[:500]:
                    query = json.loads(raw)
                    target = query.get("citation_target")
                    if (
                        isinstance(target, dict)
                        and target.get("kind") == "report"
                        and target.get("id") in visible_report_ids
                    ):
                        linked_report_ids.add(target["id"])
            else:
                subscription_scan_complete = False
                report_reference_scan_limited = True

        artifact_scan_complete = True
        if visible_report_ids and _has_table(self.conn, "knowledge_artifacts") and _has_table(
            self.conn, "knowledge_artifact_dependencies",
        ):
            if "operator" in scopes or "knowledge:read" in scopes:
                report_reference_stores.append("knowledge_artifact_dependencies")
                placeholders = ",".join("?" for _ in visible_report_ids)
                rows = self.conn.execute(
                    "SELECT d.dependency_id FROM knowledge_artifact_dependencies d "
                    "JOIN knowledge_artifacts a ON a.artifact_id=d.artifact_id "
                    f"WHERE a.namespace=? AND d.dependency_id IN ({placeholders}) "
                    "LIMIT 501",
                    [namespace, *sorted(visible_report_ids)],
                ).fetchall()
                # Keep a dense or corrupt dependency graph from making the
                # bounded Maintenance scan unbounded. If the cap is reached,
                # unlinked candidates are omitted because links may be outside it.
                report_reference_scan_limited |= len(rows) > 500
                linked_report_ids.update(row[0] for row in rows[:500])
            else:
                artifact_scan_complete = False
                report_reference_scan_limited = True

        duplicate_report_ids: set[str] = set()
        if not report_scan_limited and not citation_scope_limited:
            duplicate_groups: dict[str, list[dict[str, Any]]] = {}
            for item in report_records:
                duplicate_groups.setdefault(item["content_sha256"], []).append(item)
            for content_hash, matches in sorted(duplicate_groups.items()):
                if len(matches) < 2:
                    continue
                matches.sort(key=lambda item: item["id"])
                duplicate_report_ids.update(item["id"] for item in matches)
                first = matches[0]
                add(
                    "authored_report", first["id"], first["revision"],
                    "duplicate_report_content", "review_duplicate_reports",
                    f"Exact authored-report content is shared by {len(matches)} reports; "
                    "review the links and revisions before pruning",
                    content_hash,
                    metadata={
                        "duplicate_content_sha256": content_hash,
                        "duplicate_count": len(matches),
                        "matching_reports": [
                            {"id": item["id"], "revision": item["revision"]}
                            for item in matches[:20]
                        ],
                        "additional_match_count": max(0, len(matches) - 20),
                    },
                )

        if (
            report_scan_available
            and not report_scan_limited
            and not report_reference_scan_limited
            and not citation_scope_limited
            and subscription_scan_complete
            and artifact_scan_complete
        ):
            for item in report_records:
                if item["id"] in duplicate_report_ids or item["id"] in linked_report_ids:
                    continue
                updated_at = item["updated_at_ms"]
                if type(updated_at) is not int or now_ms < updated_at + MONTH_MS:
                    continue
                retention = {"status": "not_registered", "active_hold_ids": []}
                if "operator" not in scopes and "knowledge:retention:read" not in scopes:
                    retention = {"status": "scope_required", "active_hold_ids": None}
                elif _has_table(self.conn, "retention_objects"):
                    retention_row = self.conn.execute(
                        "SELECT status FROM retention_objects WHERE namespace=? AND object_id=?",
                        [namespace, item["id"]],
                    ).fetchone()
                    if retention_row:
                        retention = {"status": retention_row[0], "active_hold_ids": []}
                    if _has_table(self.conn, "retention_holds"):
                        holds = [row[0] for row in self.conn.execute(
                            "SELECT hold_id FROM retention_holds WHERE namespace=? "
                            "AND object_id=? AND status='active' "
                            "AND (expires_at_ms IS NULL OR expires_at_ms>?) "
                            "ORDER BY hold_id LIMIT 21",
                            [namespace, item["id"], now_ms],
                        ).fetchall()]
                        retention["active_hold_ids"] = holds[:20]
                        retention["additional_hold_count"] = max(0, len(holds) - 20)
                add(
                    "authored_report", item["id"], item["revision"],
                    "unlinked_report_candidate", "review_unlinked_report",
                    "No link was found in the accessible local intake, report-monitoring, "
                    "or artifact stores; this is a review candidate, not proof of non-use",
                    metadata={
                        "last_updated_at_ms": updated_at,
                        "checked_reference_stores": report_reference_stores,
                        "retention": retention,
                        "remote_links_checked": False,
                    },
                )

        research_covered = "operator" in scopes or "knowledge:projects:read" in scopes
        research_scope_limited = False
        research_scan_limited = False
        if research_covered and _has_table(self.conn, "research_projects"):
            rows = self.conn.execute(
                "SELECT project_id FROM research_projects WHERE namespace=? AND owner=? "
                "ORDER BY project_id LIMIT ?", [namespace, principal_id, 501],
            ).fetchall()
            research_scan_limited = len(rows) > 500
            projects = ResearchProjectStore(self.conn, initialize=False, now=self.now)
            for (project_id,) in rows[:500]:
                try:
                    project = projects.inspect(
                        namespace, project_id, principal_id=principal_id, scopes=scopes,
                    )
                except ResearchProjectError as exc:
                    if exc.code != "unauthorized":
                        raise
                    # Project scope may include another namespace or domain.
                    # Do not reveal that project's ID through a lesser-scoped queue.
                    research_scope_limited = True
                    continue
                last_updated = project.get("updated_at_ms")
                if (
                    project.get("status") in {"active", "paused"}
                    and type(last_updated) is int
                    and now_ms >= last_updated + MONTH_MS
                ):
                    add(
                        "research_project", project_id, project["revision"],
                        "stale_research_topic", "review_or_close_topic",
                        "Active research topic has not been updated for at least 30 days",
                        metadata={"status": project["status"],
                                  "last_updated_at_ms": last_updated},
                    )
                for link, availability in zip(
                    project["links"], project["reference_availability"], strict=True,
                ):
                    if link["kind"] != "intake_source" or availability["status"] not in {
                        "superseded", "unavailable",
                    }:
                        continue
                    stale = availability["status"] == "superseded"
                    add(
                        "research_project", project_id, project["revision"],
                        "superseded_research_source" if stale else "unavailable_research_source",
                        "review_new_revision" if stale else "restore_or_replace_source",
                        f"Pinned intake source {link['id']} revision {link['revision']} "
                        f"is {availability['status']}; review the project source link",
                        f"{link['id']}:{link['revision']}",
                    )

        worker_covered = "operator" in scopes or "knowledge:maintenance:admin" in scopes
        worker_scan_available = worker_covered and _has_table(
            self.conn, "knowledge_maintenance_jobs",
        )
        if worker_scan_available:
            jobs = self.conn.execute(
                "SELECT job_id,pack_id,status,attempts,available_at_ms,updated_at_ms "
                "FROM knowledge_maintenance_jobs WHERE status IN ('retry','failed','dead-letter','partial') "
                "ORDER BY updated_at_ms DESC,job_id LIMIT ?",
                [MAX_FINDINGS + 1],
            ).fetchall()
            for job_id, pack_id, status, attempts, available_at_ms, updated_at_ms in jobs:
                last_success = None
                if _has_table(self.conn, "knowledge_maintenance_generations"):
                    row = self.conn.execute(
                        "SELECT MAX(committed_at_ms) FROM knowledge_maintenance_generations "
                        "WHERE pack_id=? AND status='complete'", [pack_id],
                    ).fetchone()
                    last_success = row[0] if row else None
                suggested = (
                    "inspect_and_retry" if status in {"failed", "dead-letter"}
                    else "inspect_retry_progress" if status == "retry"
                    else "inspect_partial_generation"
                )
                add("maintenance_job", job_id, max(1, int(attempts)),
                    "failed_automation", suggested,
                    f"Source pack {pack_id} worker job is {status}; inspect its attempt history",
                    metadata={"pack_id": pack_id, "status": status,
                              "last_successful_at_ms": last_success,
                              "next_retry_at_ms": int(available_at_ms) if status == "retry" else None,
                              "updated_at_ms": int(updated_at_ms)})

        add("maintenance_review", "routine-health", 1, "routine_health_check",
            "review", "Review the configured system-health criteria")
        findings.sort(key=lambda item: (item["reason"], item["target"]["id"]))
        limitations = ["Reported repair actions are not execution receipts",
                       "Broader dependency impact is not yet composed"]
        if not worker_covered:
            limitations.append("Worker failures need maintenance admin scope to scan")
        elif not worker_scan_available:
            limitations.append("Maintenance worker state is unavailable in this database")
        if not research_covered:
            limitations.append("Pinned research sources need knowledge:projects:read to scan")
        elif research_scope_limited:
            limitations.append("Some research projects were outside current namespace or domain scope")
        if research_scan_limited:
            limitations.append("Research project scan was limited to 500 records")
        if practice_scan_limited:
            limitations.append("Practice source scan was limited to 500 packs")
        if not report_scope_available:
            limitations.append("Outdated report citations need knowledge:reports:read to scan")
        elif not report_store_available:
            limitations.append("Authored report store is unavailable for citation scanning")
        elif report_scan_limited:
            limitations.append("Authored report scan was limited to 500 reports")
            limitations.append(
                "Duplicate and unlinked report candidates were omitted because report coverage was partial"
            )
        if citation_scope_limited:
            limitations.append("Some report citations could not be compared with current evidence access")
        if (
            report_scan_available
            and (
                report_reference_scan_limited
                or not subscription_scan_complete
                or not artifact_scan_complete
                or citation_scope_limited
            )
        ):
            limitations.append(
                "Unlinked report candidates were omitted because local reference coverage was incomplete"
            )
        return {"contract": CONTRACT, "namespace": namespace, "owner": principal_id,
                "as_of_ms": now_ms, "findings": findings,
                "coverage": ["overdue_practice", "practice_intake_source_revisions", "old_draft_playbook",
                             "failed_guided_rehearsal", "stale_created_report",
                             *( ["outdated_citations"] if report_scan_available else []),
                             *( ["duplicate_report_content"]
                                if report_scan_available
                                and not report_scan_limited
                                and not citation_scope_limited else []),
                             *( ["unlinked_report_candidates"]
                                if report_scan_available
                                and not report_scan_limited
                                and not report_reference_scan_limited
                                and not citation_scope_limited
                                and subscription_scan_complete
                                and artifact_scan_complete else []),
                             *(["failed_automation"] if worker_scan_available else []),
                             *(["stale_research_topics", "pinned_research_sources"]
                               if research_covered else [])],
                "limitations": limitations}

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
