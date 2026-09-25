"""Executed Maintenance actions are preview-bound, atomic and replay-safe (#1579)."""

import json

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.intake_maintenance import MONTH_MS, IntakeMaintenanceStore
from src.kb.intake_maintenance_actions import IntakeMaintenanceActions
from src.kb.intake_maintenance_impact import IntakeMaintenanceImpact
from src.kb.intake_modes import IntakeError
from src.kb.intake_research_topic import start_research_topic
from src.kb.knowledge_retention import (
    ADMIN_SCOPE as RETENTION_ADMIN,
    EXECUTE_SCOPE as RETENTION_EXECUTE,
    READ_SCOPE as RETENTION_READ,
    KnowledgeRetentionStore,
)
from src.kb.maintenance import MaintenanceOrchestrator
from src.kb.research_projects import ResearchProjectStore
from tests.unit.kb.test_intake_maintenance import SCOPES

RECEIPT = Draft7Validator(json.load(open(
    "contracts/schemas/jsonschema/noesis-intake-maintenance-action-v1.json", encoding="utf-8")))
IMPACT = Draft7Validator(json.load(open(
    "contracts/schemas/jsonschema/noesis-intake-maintenance-impact-v1.json", encoding="utf-8")))


def _failed_job(conn, job_id="job:failed", pack="pack:one"):
    MaintenanceOrchestrator(conn, execution_mode="fixture", now=lambda: 1000)
    conn.execute(
        "INSERT INTO knowledge_maintenance_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [job_id, f"key:{job_id}", pack, 1000, "{}", "digest", "dead-letter",
         1000, None, None, None, 2, 2, False, "{}", None, 1000, 2000],
    )


def _session(conn, now, kwargs, key="monthly"):
    return IntakeMaintenanceStore(conn, now=lambda: now).start(
        "research", key, intent="Monthly review", **kwargs)


def _finding(session, reason):
    return next(item for item in session["inputs"]["findings"] if item["reason"] == reason)


def test_failed_job_refresh_requeues_once_and_replays_receipt():
    conn = duckdb.connect(":memory:")
    kwargs = {"principal_id": "alice", "scopes": SCOPES | {"knowledge:maintenance:admin"}}
    _failed_job(conn)
    session = _session(conn, 3000, kwargs)
    finding = _finding(session, "failed_automation")
    preview = IntakeMaintenanceImpact(conn, now=lambda: 3000).preview(
        "research", finding["id"], "refresh", **kwargs)
    IMPACT.validate(preview)
    actions = IntakeMaintenanceActions(conn, now=lambda: 3000)
    call = dict(expected_revision=session["revision"], finding_id=finding["id"],
                action="refresh", reviewed_preview_hash=preview["preview_hash"], **kwargs)

    executed = actions.execute("research", session["session_id"], "refresh-job", **call)
    replayed = actions.execute("research", session["session_id"], "refresh-job", **call)

    receipt = executed["maintenance_action"]
    RECEIPT.validate(receipt)
    assert receipt["basis"] == "executed"
    assert receipt["outcome"] == {"job_id": "job:failed", "status": "retry", "operation": "retry"}
    assert executed["data"]["checklist"][finding["id"]] == "done"
    assert replayed["idempotent"] is True
    audits = conn.execute(
        "SELECT count(*) FROM knowledge_maintenance_audit WHERE job_id='job:failed' AND action='retry'"
    ).fetchone()[0]
    assert audits == 1


def test_changed_impact_or_missing_scope_is_rejected_without_side_effects():
    conn = duckdb.connect(":memory:")
    admin = {"principal_id": "alice", "scopes": SCOPES | {"knowledge:maintenance:admin"}}
    _failed_job(conn)
    session = _session(conn, 3000, admin)
    finding = _finding(session, "failed_automation")
    actions = IntakeMaintenanceActions(conn, now=lambda: 3000)

    with pytest.raises(IntakeError) as error:
        actions.execute("research", session["session_id"], "stale", expected_revision=session["revision"],
                        finding_id=finding["id"], action="refresh", reviewed_preview_hash="0" * 64, **admin)
    assert error.value.code == "impact_changed"
    with pytest.raises(IntakeError) as error:
        actions.execute("research", session["session_id"], "archive-job", expected_revision=session["revision"],
                        finding_id=finding["id"], action="archive",
                        reviewed_preview_hash=IntakeMaintenanceImpact(conn, now=lambda: 3000).preview(
                            "research", finding["id"], "archive", **admin)["preview_hash"], **admin)
    assert error.value.code == "action_unsupported"
    assert conn.execute("SELECT status FROM knowledge_maintenance_jobs").fetchone()[0] == "dead-letter"


def test_stale_research_topic_archive_keeps_prior_revision_readable():
    conn = duckdb.connect(":memory:")
    scopes = SCOPES | {"knowledge:projects:read", "knowledge:projects:write"}
    kwargs = {"principal_id": "alice", "scopes": scopes}
    topic = start_research_topic(
        conn, "research", "old-topic", questions=["What changed?"],
        success_criteria=["Summarize changes"], scope={"domains": [], "namespaces": ["research"]},
        budget={"requests": 5, "tokens": 1000}, origin=None, references=[], workspace_links=None, **kwargs,
    )
    project = topic["project"]
    now = project["updated_at_ms"] + MONTH_MS
    session = _session(conn, now, kwargs)
    finding = _finding(session, "stale_research_topic")
    preview = IntakeMaintenanceImpact(conn, now=lambda: now).preview(
        "research", finding["id"], "archive", **kwargs)

    executed = IntakeMaintenanceActions(conn, now=lambda: now).execute(
        "research", session["session_id"], "archive-topic", expected_revision=session["revision"],
        finding_id=finding["id"], action="archive", reviewed_preview_hash=preview["preview_hash"], **kwargs)

    outcome = executed["maintenance_action"]["outcome"]
    assert outcome["status"] == "archived" and outcome["prior_revision"] == project["revision"]
    projects = ResearchProjectStore(conn, initialize=False)
    assert projects.inspect("research", project["project_id"], **kwargs)["status"] == "archived"
    assert projects.inspect("research", project["project_id"], revision=project["revision"], **kwargs)["status"] == "active"


def test_delete_requires_confirmation_retention_scopes_and_honours_holds():
    conn = duckdb.connect(":memory:")
    scopes = SCOPES | {"knowledge:maintenance:admin", RETENTION_ADMIN, RETENTION_READ, RETENTION_EXECUTE}
    kwargs = {"principal_id": "alice", "scopes": scopes}
    _failed_job(conn)
    retention = KnowledgeRetentionStore(conn, now=lambda: 1000)
    retention.register_policy("research", "base", 1, {"minimum_age_ms": 0},
                              principal_id="alice", scopes={RETENTION_ADMIN})
    retention.register_object("research", "pack:one", "document", "base", 1, {"id": "pack:one"},
                              created_at_ms=1000, principal_id="alice", scopes={RETENTION_ADMIN})
    hold = retention.place_hold("research", "pack:one", "legal", principal_id="alice", scopes={RETENTION_ADMIN})
    session = _session(conn, 3000, kwargs)
    finding = _finding(session, "failed_automation")
    actions = IntakeMaintenanceActions(conn, now=lambda: 3000)

    def attempt(key, confirm, auth=kwargs):
        preview = IntakeMaintenanceImpact(conn, now=lambda: 3000).preview(
            "research", finding["id"], "delete", **kwargs)
        current = IntakeMaintenanceStore(conn).conn.execute(
            "SELECT revision FROM intake_sessions WHERE session_id=?", [session["session_id"]]).fetchone()[0]
        return actions.execute("research", session["session_id"], key, expected_revision=current,
                               finding_id=finding["id"], action="delete",
                               reviewed_preview_hash=preview["preview_hash"], confirm_delete=confirm, **auth)

    for key, confirm, auth, code in (
        ("unconfirmed", False, kwargs, "confirmation_required"),
        ("no-scope", True, {"principal_id": "alice", "scopes": scopes - {RETENTION_EXECUTE}}, "unauthorized"),
        ("held", True, kwargs, "deletion_blocked"),
    ):
        with pytest.raises(IntakeError) as error:
            attempt(key, confirm, auth)
        assert error.value.code == code
    retention.release_hold("research", hold["hold_id"], principal_id="alice", scopes={RETENTION_ADMIN})

    deleted = attempt("delete-pack", True)

    outcome = deleted["maintenance_action"]["outcome"]
    assert outcome["tombstoned"] == ["pack:one"] and outcome["status"] == "completed"
    assert conn.execute(
        "SELECT status FROM retention_objects WHERE object_id='pack:one'").fetchone()[0] == "tombstoned"


def test_superseded_research_source_repair_repins_current_revision():
    from src.kb.intake_exploration import IntakeExplorationStore
    from src.kb.intake_modes import IntakeStore
    from tests.unit.kb.test_intake_research_topic import SCOPES as TOPIC_SCOPES, _start

    conn = duckdb.connect(":memory:")
    kwargs = {"principal_id": "alice", "scopes": TOPIC_SCOPES}
    exploration = IntakeExplorationStore(conn)
    explore = IntakeStore(conn).create("research", "Exploration", "saved", intent="Find", **kwargs)
    first = exploration.capture("research", explore["session_id"], "one", expected_revision=1,
                                url="https://example.org/r", title="Original", content="Original text",
                                saved=True, **kwargs)
    project = _start(conn, "topic", references=[first["references"][0]])["project"]
    exploration.capture("research", explore["session_id"], "two", expected_revision=2,
                        url="https://example.org/r", title="Corrected", content="Corrected text",
                        saved=True, **kwargs)
    session = _session(conn, 5000, kwargs)
    finding = _finding(session, "superseded_research_source")
    preview = IntakeMaintenanceImpact(conn, now=lambda: 5000).preview(
        "research", finding["id"], "refresh", **kwargs)

    repaired = IntakeMaintenanceActions(conn, now=lambda: 5000).execute(
        "research", session["session_id"], "repin", expected_revision=session["revision"],
        finding_id=finding["id"], action="repair", reviewed_preview_hash=preview["preview_hash"], **kwargs)

    outcome = repaired["maintenance_action"]["outcome"]
    RECEIPT.validate(repaired["maintenance_action"])
    assert (outcome["previous_revision"], outcome["repinned_revision"]) == (1, 2)
    projects = ResearchProjectStore(conn, initialize=False)
    current = projects.inspect("research", project["project_id"], **kwargs)
    assert current["reference_availability"][0]["status"] == "current"
    assert projects.inspect("research", project["project_id"], revision=project["revision"],
                            **kwargs)["links"][0]["revision"] == 1
    rescanned = IntakeMaintenanceStore(conn, now=lambda: 5000).scan("research", **kwargs)
    assert not any(item["reason"] == "superseded_research_source" for item in rescanned["findings"])
