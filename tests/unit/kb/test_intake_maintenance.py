"""Maintenance findings are explainable; a recorded fix is not executed proof."""

import duckdb
import pytest

from src.kb.authored_reports import AuthoredReportStore
from src.kb.intake_creation import IntakeCreationStore
from src.kb.intake_maintenance import IntakeMaintenanceStore
from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_playbooks import IntakePlaybookStore
from src.kb.intake_practice import IntakePracticeStore
from src.kb.intake_problem import IntakeProblemStore

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
    problem.record_step("research", opened["session_id"], "observed", expected_revision=1,
                        kind="verification", summary="Search again",
                        observation="New item visible", passed=True, **kwargs)
    IntakeStore(conn, now=lambda: 1000).command(
        "research", opened["session_id"], "resolve", expected_revision=2,
        action="complete", payload=None, **kwargs)
    playbook = IntakePlaybookStore(conn, now=lambda: 1000).promote_problem(
        "research", opened["session_id"], "guide", title="Repair search",
        prerequisites=[], environment="Desktop",
        steps=[{"action": "Restart worker", "expected_result": "Item visible",
                "recovery": "Inspect logs"}], verification="New item visible",
        source_rationale="Observed repair", **kwargs)
    maintenance = IntakeMaintenanceStore(conn, now=lambda: 31 * DAY)
    queue = maintenance.scan("research", **kwargs)
    assert {item["reason"] for item in queue["findings"]} == {
        "overdue_practice", "old_draft_playbook", "routine_health_check",
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
