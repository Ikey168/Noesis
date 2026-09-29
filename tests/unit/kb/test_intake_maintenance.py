"""Maintenance findings are explainable; a recorded fix is not executed proof."""

import json

import duckdb
import pytest

from src.kb.authored_reports import AuthoredReportStore
from src.kb.intake_creation import IntakeCreationStore
from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_maintenance import MONTH_MS, IntakeMaintenanceStore
from src.kb.intake_maintenance_impact import IntakeMaintenanceImpact
from src.kb.maintenance import MaintenanceOrchestrator
from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_playbooks import IntakePlaybookStore
from src.kb.intake_practice import IntakePracticeStore
from src.kb.intake_problem import IntakeProblemStore
from src.kb.knowledge_retention import ADMIN_SCOPE as RETENTION_ADMIN, READ_SCOPE as RETENTION_READ
from src.kb.knowledge_retention import KnowledgeRetentionStore
from src.kb.intake_research_topic import start_research_topic
from src.ingestion.revisions import DocumentRevisionStore

DAY = 86_400_000
SCOPES = {
    "knowledge:intake:read", "knowledge:intake:write",
    "namespace:research:read", "namespace:research:write",
    "namespace:archive:read",
    "knowledge:reports:read", "knowledge:reports:write",
}


def _report(title: str) -> dict:
    return {"title": title,
            "snapshot": {"id": "snapshot:one", "generations": {"research": 1}},
            "sections": [{"id": "section:one", "title": "Summary", "assertions": [{
                "id": "assertion:one", "text": "Guide", "kind": "commentary",
                "dependencies": [], "citations": [],
            }]}], "bibliography": [], "limitations": ["Author reviewed"]}


def test_maintenance_snapshot_replay_and_typed_completion(tmp_path):
    path = str(tmp_path / "maintenance.duckdb")
    conn = duckdb.connect(path)
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
    practice = IntakePracticeStore(conn, now=lambda: 1000)
    pack = practice.create_pack("research", "practice-key", "Index worker", [{
        "kind": "recall", "prompt": "Which worker stopped?", "answer": "The index worker",
        "mastery_criterion": "Name the worker without notes",
        "references": [{"kind": "concept", "id": "concept:worker",
                        "namespace": "archive", "version": 1}],
    }], **kwargs)
    problem = IntakeProblemStore(conn, now=lambda: 1000)
    opened = problem.start("research", "broken-search", symptom="Search stale",
                           environment="Desktop", urgency="blocking",
                           success_check="New item visible", **kwargs)
    verification_session = IntakeStore(conn, now=lambda: 1000).create(
        "research", "Exploration", "maintenance-verification-source",
        intent="Capture verification evidence", principal_id="alice", scopes=SCOPES,
    )
    verification_source = IntakeExplorationStore(conn, now=lambda: 1000).capture(
        "research", verification_session["session_id"], "maintenance-verification",
        expected_revision=1, url="https://example.org/maintenance-verification",
        title="Verification observation", content="Observed the expected result.",
        principal_id="alice", scopes=SCOPES,
    )
    problem.record_step("research", opened["session_id"], "observed", expected_revision=1,
                        kind="verification", summary="Search again",
                        observation="New item visible", passed=True,
                        references=verification_source["references"], **kwargs)
    IntakeStore(conn, now=lambda: 1000).command(
        "research", opened["session_id"], "resolve", expected_revision=2,
        action="complete", payload=None, **kwargs)
    playbook = IntakePlaybookStore(conn, now=lambda: 1000).promote_problem(
        "research", opened["session_id"], "guide", title="Repair search",
        prerequisites=[], environment="Desktop",
        steps=[{"action": "Restart worker", "expected_result": "Item visible",
                "recovery": "Inspect logs"}], verification="New item visible",
        source_rationale="Observed repair", **kwargs)
    runs = IntakePlaybookStore(conn, now=lambda: 1000)
    run = runs.start_run("research", playbook["playbook_id"], "rehearsal",
                         playbook_revision=1, environment="Desktop", **kwargs)
    runs.command_run("research", run["run_id"], "failed-step",
                     expected_revision=1, action="step",
                     payload={"step_id": "step-1", "passed": False,
                              "observation": "Item still missing"}, **kwargs)
    maintenance = IntakeMaintenanceStore(conn, now=lambda: 31 * DAY)
    queue = maintenance.scan("research", **kwargs)
    assert {item["reason"] for item in queue["findings"]} == {
        "overdue_practice", "old_draft_playbook", "failed_guided_rehearsal",
        "routine_health_check",
    }
    assert all("answer" not in item for item in queue["findings"])
    session = maintenance.start("research", "monthly", intent="Review system health",
                                duration_minutes=45, **kwargs)
    assert session["mode"] == "Maintenance"
    assert maintenance.start("research", "monthly", intent="Review system health",
                             duration_minutes=45, **kwargs)["idempotent"]
    with pytest.raises(IntakeError, match="typed troubleshooting|record_maintenance"):
        IntakeStore(conn, now=lambda: 31 * DAY).command(
            "research", session["session_id"], "bypass", expected_revision=1,
            action="record", payload={"data": {"checklist": {"fake": "done"},
                                               "health_acceptable": True}}, **kwargs)
    current = session
    for item in queue["findings"]:
        current = maintenance.record_finding(
            "research", session["session_id"], "review-" + item["id"],
            expected_revision=current["revision"], finding_id=item["id"],
            action="defer" if item["reason"] == "overdue_practice" else "reviewed",
            observation="Reviewed with a monthly checklist", **kwargs,
        )
    assert current["data"]["checklist"][queue["findings"][0]["id"]] in {"done", "deferred"}
    with pytest.raises(IntakeError, match="reviewed_health_criteria"):
        IntakeStore(conn, now=lambda: 31 * DAY).command(
            "research", session["session_id"], "early-complete",
            expected_revision=current["revision"], action="complete", payload=None, **kwargs)
    healthy = maintenance.assess_health(
        "research", session["session_id"], "health", expected_revision=current["revision"],
        acceptable=True, criteria="No unresolved blocking failures",
        observation="No blocking failures found", **kwargs,
    )
    assert healthy["unmet_completion_checks"] == []
    assert maintenance.assess_health(
        "research", session["session_id"], "health", expected_revision=current["revision"],
        acceptable=True, criteria="No unresolved blocking failures",
        observation="No blocking failures found", **kwargs,
    )["idempotent"]
    completed = IntakeStore(conn, now=lambda: 31 * DAY).command(
        "research", session["session_id"], "complete", expected_revision=healthy["revision"],
        action="complete", payload=None, **kwargs,
    )
    assert completed["status"] == "completed"
    assert completed["data"]["maintenance_reviews"]
    conn.close()

    conn = duckdb.connect(path)
    revoked = IntakeMaintenanceStore(conn, now=lambda: 32 * DAY).scan(
        "research", principal_id="alice", scopes=SCOPES - {"namespace:archive:read"},
    )
    assert any(item["reason"] == "practice_source_access_unavailable"
               and item["target"]["id"] == pack["pack_id"] for item in revoked["findings"])
    assert any(item["target"]["id"] == playbook["playbook_id"]
               for item in revoked["findings"])
    bob = IntakeMaintenanceStore(conn, now=lambda: 32 * DAY).scan(
        "research", principal_id="bob", scopes=SCOPES,
    )
    assert [item["reason"] for item in bob["findings"]] == ["routine_health_check"]


def test_maintenance_flags_a_finished_creation_after_its_report_changes():
    conn = duckdb.connect(":memory:")
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
    reports = AuthoredReportStore(conn, now=lambda: 1000)
    report = reports.create("research", "report", _report("Guide v1"), **kwargs)
    creations = IntakeCreationStore(conn, now=lambda: 1000)
    project = creations.create("research", "project", title="Guide", audience="Operators",
                               artifact_type="documentation", purpose="Explain repair",
                               criteria=["Usable"], inputs=[], workspace_links=[], **kwargs)
    project_id = project["project_id"]
    creations.command("research", project_id, "attach", expected_revision=1,
                      action="attach_report", payload={"report_id": report["report_id"],
                                                       "revision": 1}, **kwargs)
    creations.command("research", project_id, "review", expected_revision=2,
                      action="review", payload={"checks": {"Usable": True},
                                                "notes": "Reviewed"}, **kwargs)
    creations.command("research", project_id, "finish", expected_revision=3,
                      action="finish", payload={}, **kwargs)
    assert [item["reason"] for item in IntakeMaintenanceStore(
        conn, now=lambda: 2000,
    ).scan("research", **kwargs)["findings"]] == ["routine_health_check"]
    reports.revise("research", report["report_id"], 1, _report("Guide v2"), **kwargs)
    findings = IntakeMaintenanceStore(conn, now=lambda: 2000).scan(
        "research", **kwargs,
    )["findings"]
    assert any(item["reason"] == "stale_created_report"
               and item["target"]["id"] == project_id for item in findings)


def test_maintenance_reviews_corrected_research_sources_without_leaking_other_projects():
    conn = duckdb.connect(":memory:")
    scopes = SCOPES | {"knowledge:projects:read", "knowledge:projects:write"}
    kwargs = {"principal_id": "alice", "scopes": scopes}
    exploration = IntakeExplorationStore(conn, now=lambda: 1000)
    visited = IntakeStore(conn, now=lambda: 1000).create(
        "research", "Exploration", "source-session", intent="Save evidence", **kwargs,
    )
    first = exploration.capture(
        "research", visited["session_id"], "first", expected_revision=1,
        url="https://example.org/study", title="Study", content="First version",
        saved=True, **kwargs,
    )
    ref = first["references"][0]
    topic = start_research_topic(
        conn, "research", "topic", questions=["What changed?"],
        success_criteria=["List supported and unresolved points"],
        scope={"domains": [], "namespaces": ["research"]},
        budget={"requests": 5, "tokens": 1000},
        origin=None, references=[ref], workspace_links=None, **kwargs,
    )
    maintenance = IntakeMaintenanceStore(conn, now=lambda: 2000)
    assert [finding["reason"] for finding in maintenance.scan(
        "research", **kwargs,
    )["findings"]] == ["routine_health_check"]

    exploration.capture(
        "research", visited["session_id"], "corrected", expected_revision=2,
        url="https://example.org/study", title="Corrected study",
        content="Corrected version", saved=True, **kwargs,
    )
    queue = maintenance.scan("research", **kwargs)
    finding = next(item for item in queue["findings"]
                   if item["reason"] == "superseded_research_source")
    assert finding["target"] == {
        "kind": "research_project", "id": topic["project"]["project_id"],
        "namespace": "research", "version": 1,
    }
    assert ref["id"] in finding["detail"]
    assert "Corrected version" not in json.dumps(queue)
    assert "pinned_research_sources" in queue["coverage"]
    session = maintenance.start("research", "review-correction", intent="Review sources", **kwargs)
    assert finding in session["inputs"]["findings"]
    assert finding["target"] in session["references"]

    without_project_scope = maintenance.scan(
        "research", principal_id="alice", scopes=SCOPES,
    )
    assert [item["reason"] for item in without_project_scope["findings"]] == ["routine_health_check"]
    assert "pinned_research_sources" not in without_project_scope["coverage"]
    assert "knowledge:projects:read" in " ".join(without_project_scope["limitations"])
    assert [item["reason"] for item in maintenance.scan(
        "research", principal_id="bob", scopes=scopes,
    )["findings"]] == ["routine_health_check"]

    conn.execute(
        "DELETE FROM intake_exploration_source_revisions "
        "WHERE namespace=? AND owner=? AND source_id=? AND version=1",
        ["research", "alice", ref["id"]],
    )
    missing = maintenance.scan("research", **kwargs)
    assert any(item["reason"] == "unavailable_research_source"
               and item["target"]["id"] == topic["project"]["project_id"]
               for item in missing["findings"])


def test_maintenance_flags_only_stale_open_research_topics_with_project_access():
    conn = duckdb.connect(":memory:")
    scopes = SCOPES | {"knowledge:projects:read", "knowledge:projects:write"}
    kwargs = {"principal_id": "alice", "scopes": scopes}
    topic = start_research_topic(
        conn, "research", "old-topic", questions=["What changed in the policy?"],
        success_criteria=["Summarize confirmed and unresolved changes"],
        scope={"domains": [], "namespaces": ["research"]},
        budget={"requests": 5, "tokens": 1000}, origin=None,
        references=[], workspace_links=None, **kwargs,
    )
    project = topic["project"]
    threshold = project["updated_at_ms"] + MONTH_MS

    before_threshold = IntakeMaintenanceStore(
        conn, now=lambda: threshold - 1,
    ).scan("research", **kwargs)
    assert not any(item["reason"] == "stale_research_topic"
                   for item in before_threshold["findings"])

    stale = IntakeMaintenanceStore(conn, now=lambda: threshold).scan(
        "research", **kwargs,
    )
    finding = next(item for item in stale["findings"]
                   if item["reason"] == "stale_research_topic")
    assert finding["target"] == {
        "kind": "research_project", "id": project["project_id"],
        "namespace": "research", "version": project["revision"],
    }
    assert finding["metadata"] == {
        "status": "active", "last_updated_at_ms": project["updated_at_ms"],
    }
    assert "stale_research_topics" in stale["coverage"]

    without_project_scope = IntakeMaintenanceStore(
        conn, now=lambda: threshold,
    ).scan("research", principal_id="alice", scopes=SCOPES)
    assert not any(item["reason"] == "stale_research_topic"
                   for item in without_project_scope["findings"])
    assert "stale_research_topics" not in without_project_scope["coverage"]
    conn.close()


def test_maintenance_finds_outdated_report_citations_without_copying_source_content():
    conn = duckdb.connect(":memory:")
    scopes = SCOPES | {"document:doc-1:read"}
    kwargs = {"principal_id": "alice", "scopes": scopes}
    sources = DocumentRevisionStore(conn)
    first_source = sources.observe({
        "document_id": "doc-1", "content": "Original evidence.", "metadata": {},
    })
    conn.execute(
        "UPDATE document_revision_records SET committed_watermark=1 WHERE revision_id=?",
        [first_source["revision_id"]],
    )
    report_content = {
        "title": "Reviewed report",
        "snapshot": {"id": "snapshot:maintenance", "generations": {"research": 1}},
        "sections": [{"id": "summary", "title": "Summary", "assertions": [{
            "id": "claim:one", "text": "The original finding.", "kind": "sourced",
            "citations": ["source:one"],
            "dependencies": [{
                "kind": "source", "id": "doc-1", "revision": first_source["revision_id"],
                "namespace": "research",
                "locator": {"document_id": "doc-1", "revision_id": first_source["revision_id"]},
            }],
        }]}],
        "bibliography": [{"id": "source:one", "text": "Original source."}],
        "limitations": ["Authored source claim."],
    }
    report = AuthoredReportStore(conn).create(
        "research", "maintenance-citation", report_content, **kwargs,
    )
    assert not any(item["reason"] == "outdated_citation" for item in
                   IntakeMaintenanceStore(conn, now=lambda: 1000).scan("research", **kwargs)["findings"])

    revised_source = sources.observe({
        "document_id": "doc-1", "content": "Corrected evidence content.", "metadata": {},
    })
    conn.execute(
        "UPDATE document_revision_records SET committed_watermark=2 WHERE revision_id=?",
        [revised_source["revision_id"]],
    )
    queue = IntakeMaintenanceStore(conn, now=lambda: 2000).scan("research", **kwargs)
    finding = next(item for item in queue["findings"]
                   if item["reason"] == "outdated_citation")
    assert finding["target"] == {
        "kind": "authored_report", "id": report["report_id"],
        "namespace": "research", "version": 1,
    }
    assert finding["metadata"] == {
        "section_id": "summary", "assertion_id": "claim:one", "source_id": "doc-1",
        "pinned_revision": first_source["revision_id"],
        "current_revision": revised_source["revision_id"],
        "source_status": "active", "comparison_reason": "revision_changed",
    }
    assert "outdated_citations" in queue["coverage"]
    assert "Corrected evidence content" not in json.dumps(queue)

    revoked = IntakeMaintenanceStore(conn, now=lambda: 2000).scan(
        "research", principal_id="alice",
        scopes=scopes - {"document:doc-1:read"},
    )
    assert not any(item["reason"] == "outdated_citation" for item in revoked["findings"])
    assert any("current evidence access" in item for item in revoked["limitations"])
    conn.close()


def test_maintenance_reports_duplicate_and_unlinked_reports_with_owner_and_link_scoping():
    conn = duckdb.connect(":memory:")
    reports = AuthoredReportStore(conn, now=lambda: 1000)
    alice = {"principal_id": "alice", "scopes": SCOPES}
    duplicate_one = reports.create("research", "duplicate-one", _report("Same"), **alice)
    duplicate_two = reports.create("research", "duplicate-two", _report("Same"), **alice)
    unlinked = reports.create("research", "old-unlinked", _report("Old reference-free"), **alice)
    linked = reports.create("research", "old-linked", _report("Referenced"), **alice)
    bob_report = reports.create(
        "research", "bob-duplicate", _report("Same"),
        principal_id="bob", scopes=SCOPES,
    )
    IntakeStore(conn, now=lambda: 1000).create(
        "research", "Exploration", "references-authored-report",
        intent="Keep this report in use",
        references=[{"kind": "authored_report", "id": linked["report_id"],
                     "namespace": "research", "version": 1}],
        **alice,
    )

    queue = IntakeMaintenanceStore(conn, now=lambda: 1000 + MONTH_MS).scan(
        "research", **alice,
    )
    duplicates = [item for item in queue["findings"]
                  if item["reason"] == "duplicate_report_content"]
    unused = [item for item in queue["findings"]
              if item["reason"] == "unlinked_report_candidate"]

    assert len(duplicates) == 1
    assert duplicates[0]["metadata"]["duplicate_count"] == 2
    assert {item["id"] for item in duplicates[0]["metadata"]["matching_reports"]} == {
        duplicate_one["report_id"], duplicate_two["report_id"],
    }
    assert [item["target"]["id"] for item in unused] == [unlinked["report_id"]]
    assert unused[0]["metadata"]["remote_links_checked"] is False
    assert unused[0]["metadata"]["retention"] == {
        "status": "scope_required", "active_hold_ids": None,
    }
    assert linked["report_id"] not in json.dumps(unused)
    assert bob_report["report_id"] not in json.dumps(queue)
    assert "duplicate_report_content" in queue["coverage"]
    assert "unlinked_report_candidates" in queue["coverage"]
    conn.close()


def test_maintenance_unlinked_report_candidate_includes_current_retention_hold():
    conn = duckdb.connect(":memory:")
    now_ms = 40 * DAY
    retention_admin = SCOPES | {RETENTION_ADMIN, RETENTION_READ}
    reports = AuthoredReportStore(conn, now=lambda: 1000)
    report = reports.create("research", "old-report", _report("Old"),
                            principal_id="alice", scopes=SCOPES)
    retention = KnowledgeRetentionStore(conn, now=lambda: 1000)
    retention.register_policy(
        "research", "reports", 1, {"minimum_age_ms": 0},
        principal_id="alice", scopes={RETENTION_ADMIN},
    )
    retention.register_object(
        "research", report["report_id"], "document", "reports", 1,
        {"report_id": report["report_id"]}, created_at_ms=1000,
        principal_id="alice", scopes={RETENTION_ADMIN},
    )
    hold = retention.place_hold(
        "research", report["report_id"], "preserve review history",
        principal_id="alice", scopes={RETENTION_ADMIN}, expires_at_ms=now_ms + DAY,
    )
    queue = IntakeMaintenanceStore(conn, now=lambda: now_ms).scan(
        "research", principal_id="alice", scopes=retention_admin,
    )
    finding = next(item for item in queue["findings"]
                   if item["reason"] == "unlinked_report_candidate")
    assert finding["metadata"]["retention"] == {
        "status": "active", "active_hold_ids": [hold["hold_id"]],
        "additional_hold_count": 0,
    }

    retention.place_hold(
        "research", report["report_id"], "already expired",
        principal_id="alice", scopes={RETENTION_ADMIN}, expires_at_ms=now_ms,
    )
    refreshed = IntakeMaintenanceStore(conn, now=lambda: now_ms).scan(
        "research", principal_id="alice", scopes=retention_admin,
    )
    finding = next(item for item in refreshed["findings"]
                   if item["reason"] == "unlinked_report_candidate")
    assert finding["metadata"]["retention"]["active_hold_ids"] == [hold["hold_id"]]
    conn.close()


def test_maintenance_worker_failures_are_scoped_and_survive_restart(tmp_path):
    path = str(tmp_path / "worker-review.duckdb")
    conn = duckdb.connect(path)
    MaintenanceOrchestrator(conn, execution_mode="fixture", now=lambda: 1000)
    conn.execute(
        "INSERT INTO knowledge_maintenance_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ["job:failed", "key:failed", "pack:one", 1000, "{}", "digest", "dead-letter",
         1000, None, None, None, 2, 2, False, "{}", None, 1000, 2000],
    )
    conn.close()
    conn = duckdb.connect(path)
    admin = SCOPES | {"knowledge:maintenance:admin"}
    scan = IntakeMaintenanceStore(conn, now=lambda: 3000).scan(
        "research", principal_id="alice", scopes=admin,
    )
    failure = next(item for item in scan["findings"] if item["reason"] == "failed_automation")
    assert failure["target"]["id"] == "job:failed"
    assert failure["metadata"]["status"] == "dead-letter"
    assert failure["metadata"]["last_successful_at_ms"] is None
    assert "failed_automation" in scan["coverage"]
    ordinary = IntakeMaintenanceStore(conn, now=lambda: 3000).scan(
        "research", principal_id="alice", scopes=SCOPES,
    )
    assert not any(item["reason"] == "failed_automation" for item in ordinary["findings"])
    assert "failed_automation" not in ordinary["coverage"]


def test_failed_worker_impact_resolves_source_pack_and_retention_dependents():
    conn = duckdb.connect(":memory:")
    scopes = SCOPES | {
        "knowledge:maintenance:admin", RETENTION_ADMIN, RETENTION_READ,
    }
    kwargs = {"principal_id": "alice", "scopes": scopes}
    MaintenanceOrchestrator(conn, execution_mode="fixture", now=lambda: 1000)
    conn.execute(
        "INSERT INTO knowledge_maintenance_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ["job:failed", "key:failed", "pack:one", 1000, "{}", "digest", "dead-letter",
         1000, None, None, None, 2, 2, False, "{}", None, 1000, 2000],
    )
    session = IntakeStore(conn, now=lambda: 1000).create(
        "research", "Exploration", "depends-on-source-pack", intent="Review source pack",
        references=[{"kind": "source_pack", "id": "pack:one",
                     "namespace": "research", "version": 1}], **kwargs,
    )
    retention = KnowledgeRetentionStore(conn, now=lambda: 1000)
    retention.register_policy(
        "research", "base", 1, {"minimum_age_ms": 0},
        principal_id="alice", scopes={RETENTION_ADMIN},
    )
    retention.register_object(
        "research", "pack:one", "document", "base", 1, {"id": "pack:one"},
        created_at_ms=1000, principal_id="alice", scopes={RETENTION_ADMIN},
    )
    retention.register_object(
        "research", "derived:one", "document", "base", 1, {"id": "derived:one"},
        created_at_ms=1000, dependencies=["pack:one"],
        principal_id="alice", scopes={RETENTION_ADMIN},
    )
    hold = retention.place_hold(
        "research", "pack:one", "preserve source provenance",
        principal_id="alice", scopes={RETENTION_ADMIN},
    )

    queue = IntakeMaintenanceStore(conn, now=lambda: 3000).scan("research", **kwargs)
    finding = next(item for item in queue["findings"]
                   if item["reason"] == "failed_automation")
    preview = IntakeMaintenanceImpact(conn, now=lambda: 3000).preview(
        "research", finding["id"], "delete", **kwargs,
    )
    assert preview["target"]["id"] == "job:failed"
    assert preview["impact_root"] == {
        "kind": "source_pack", "id": "pack:one", "namespace": "research",
    }
    assert {item["id"] for item in preview["affected"]} == {
        session["session_id"], "derived:one",
    }
    assert preview["retention"] == {
        "status": "active", "dependencies": [], "pins": [],
        "active_hold_ids": [hold["hold_id"]], "generation": 0,
        "object_class": "document",
    }
    assert preview["deletion_authorized"] is False
    conn.close()


def test_maintenance_impact_preview_is_scoped_read_only_and_names_dependents():
    conn = duckdb.connect(":memory:")
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
    pack = IntakePracticeStore(conn, now=lambda: 1000).create_pack(
        "research", "pack-impact", "Worker", [{
            "kind": "recall", "prompt": "Which worker?", "answer": "Index worker",
            "mastery_criterion": "Name it without notes",
            "references": [{"kind": "concept", "id": "concept:worker",
                            "namespace": "research", "version": 1}],
        }], **kwargs,
    )
    session = IntakeStore(conn, now=lambda: 1000).create(
        "research", "Exploration", "depends-on-pack", intent="Review the pack",
        references=[{"kind": "practice_pack", "id": pack["pack_id"],
                     "namespace": "research", "version": 1}], **kwargs,
    )
    finding = next(item for item in IntakeMaintenanceStore(conn, now=lambda: 3 * DAY).scan(
        "research", **kwargs,
    )["findings"] if item["reason"] == "overdue_practice")
    impact = IntakeMaintenanceImpact(conn, now=lambda: 3 * DAY)
    preview = impact.preview("research", finding["id"], "delete", **kwargs)
    assert preview["impact_root"] == preview["target"]
    assert {item["id"] for item in preview["affected"]} == {session["session_id"]}
    assert preview["deletion_authorized"] is False
    assert preview["retention"]["status"] == "scope_required"
    with pytest.raises(IntakeError, match="visible"):
        impact.preview("research", finding["id"], "delete",
                       principal_id="bob", scopes=SCOPES)
