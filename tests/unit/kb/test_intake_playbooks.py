"""Problem promotion and guided procedures retain version and outcome distinctions."""

import duckdb
import pytest

from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_playbooks import IntakePlaybookStore
from src.kb.intake_problem import IntakeProblemStore

SCOPES = {
    "knowledge:intake:read", "knowledge:intake:write",
    "namespace:research:read", "namespace:research:write",
    "namespace:evidence:read",
}
STEPS = [
    {"action": "Inspect index status", "expected_result": "Index is stale",
     "recovery": "Stop and inspect logs"},
    {"action": "Rebuild index", "expected_result": "Index becomes current",
     "recovery": "Restore prior index and inspect logs"},
]
PLUGIN_LINK = {
    "workspace_id": "personal", "account_id": "alice",
    "plugin_id": "executable-runbooks", "collection": "incidents",
    "record_id": "incident-73", "authoritative_version": 2,
    "representation": "intentional_snapshot", "authority": "noesis",
    "noesis_reference": {"kind": "concept", "id": "concept:recovery",
                         "namespace": "evidence", "version": 1},
}


def _verified_problem(conn):
    problem = IntakeProblemStore(conn, now=lambda: 1000)
    opened = problem.start(
        "research", "incident-1", symptom="Search stale", environment="Desktop 2.7",
        urgency="blocking", success_check="New item appears within a minute",
        plugin_links=[PLUGIN_LINK],
        principal_id="alice", scopes=SCOPES,
    )
    verification_session = IntakeStore(conn, now=lambda: 1000).create(
        "research", "Exploration", "playbook-verification-source",
        intent="Capture verification evidence", principal_id="alice", scopes=SCOPES,
    )
    verification_source = IntakeExplorationStore(conn, now=lambda: 1000).capture(
        "research", verification_session["session_id"], "playbook-verification",
        expected_revision=1, url="https://example.org/playbook-verification",
        title="Verification observation", content="Observed the expected result.",
        principal_id="alice", scopes=SCOPES,
    )
    checked = problem.record_step(
        "research", opened["session_id"], "checked", expected_revision=1,
        kind="verification", summary="Search for new item",
        observation="Item appeared after 12 seconds", passed=True,
        references=verification_source["references"],
        principal_id="alice", scopes=SCOPES,
    )
    IntakeStore(conn).command(
        "research", opened["session_id"], "complete", expected_revision=2,
        action="complete", payload=None, principal_id="alice", scopes=SCOPES,
    )
    return checked["session_id"]


def _promote(store, problem_id, *, scopes=SCOPES):
    return store.promote_problem(
        "research", problem_id, "playbook-1", title="Refresh stale search",
        prerequisites=[], environment="Desktop 2.7", steps=STEPS,
        verification="New item appears within a minute",
        source_rationale="Derived from the verified troubleshooting session",
        principal_id="alice", scopes=scopes,
    )


def test_problem_playbook_guided_failure_recovery_and_replay(tmp_path):
    path = str(tmp_path / "playbooks.duckdb")
    conn = duckdb.connect(path)
    store = IntakePlaybookStore(conn, now=lambda: 2000)
    with pytest.raises(IntakeError, match="session revision"):
        _promote(store, "intake:missing")
    problem_id = _verified_problem(conn)
    created = _promote(store, problem_id)
    assert created["trust_state"] == "draft"
    assert created["origin"]["session_id"] == problem_id
    assert created["plugin_links"] == [PLUGIN_LINK]
    with pytest.raises(IntakeError, match="plugin-linked playbook sources"):
        store.inspect("research", created["playbook_id"], principal_id="alice",
                      scopes=SCOPES - {"namespace:evidence:read"})
    conn.close()
    reopened = duckdb.connect(path)
    store = IntakePlaybookStore(reopened, now=lambda: 3000)
    assert _promote(store, problem_id)["idempotent"] is True
    playbook_id = created["playbook_id"]
    with pytest.raises(IntakeError, match="owner"):
        store.inspect("research", playbook_id, principal_id="bob", scopes=SCOPES)
    revised = store.revise(
        "research", playbook_id, "edit-1", expected_revision=1,
        title="Refresh stale search safely", prerequisites=[],
        environment="Desktop 2.7", steps=STEPS,
        verification="New item appears within a minute",
        source_rationale="Derived from the verified troubleshooting session",
        principal_id="alice", scopes=SCOPES,
    )
    assert revised["revision"] == 2
    assert store.inspect(
        "research", playbook_id, revision=1,
        principal_id="alice", scopes=SCOPES,
    )["title"] == "Refresh stale search"
    assert store.revise(
        "research", playbook_id, "edit-1", expected_revision=1,
        title="Refresh stale search safely", prerequisites=[],
        environment="Desktop 2.7", steps=STEPS,
        verification="New item appears within a minute",
        source_rationale="Derived from the verified troubleshooting session",
        principal_id="alice", scopes=SCOPES,
    )["idempotent"] is True
    run = store.start_run(
        "research", playbook_id, "run-1", playbook_revision=2,
        environment="Desktop 2.7 on laptop", principal_id="alice", scopes=SCOPES,
    )
    with pytest.raises(IntakeError, match="complete all steps"):
        store.command_run(
            "research", run["run_id"], "early", expected_revision=1,
            action="verify", payload={"passed": True, "observation": "Worked"},
            principal_id="alice", scopes=SCOPES,
        )
    failed = store.command_run(
        "research", run["run_id"], "step-1-failed", expected_revision=1,
        action="step", payload={"step_id": "step-1", "passed": False,
            "observation": "Status endpoint unavailable"},
        principal_id="alice", scopes=SCOPES,
    )
    assert failed["next_step"] == 0
    assert revised["steps"][0]["recovery"] == "Stop and inspect logs"
    passed = store.command_run(
        "research", run["run_id"], "step-1-passed", expected_revision=2,
        action="step", payload={"step_id": "step-1", "passed": True,
            "observation": "Index is stale"},
        principal_id="alice", scopes=SCOPES,
    )
    assert passed["next_step"] == 1
    with pytest.raises(IntakeError, match="another action"):
        store.command_run(
            "research", run["run_id"], "step-1-passed", expected_revision=2,
            action="step", payload={"step_id": "step-1", "passed": False,
                "observation": "Index is stale"},
            principal_id="alice", scopes=SCOPES,
        )
    store.command_run(
        "research", run["run_id"], "step-2", expected_revision=3,
        action="step", payload={"step_id": "step-2", "passed": True,
            "observation": "Index became current"},
        principal_id="alice", scopes=SCOPES,
    )
    failed_check = store.command_run(
        "research", run["run_id"], "verify-failed", expected_revision=4,
        action="verify", payload={"passed": False,
            "observation": "New item still absent"},
        principal_id="alice", scopes=SCOPES,
    )
    assert failed_check["status"] == "active"
    reopened.close()

    conn = duckdb.connect(path)
    store = IntakePlaybookStore(conn, now=lambda: 4000)
    completed = store.command_run(
        "research", run["run_id"], "verify-passed", expected_revision=5,
        action="verify", payload={"passed": True,
            "observation": "New item appears after 12 seconds"},
        principal_id="alice", scopes=SCOPES,
    )
    assert completed["status"] == "completed"
    assert len(completed["observations"]) == 3
    assert store.inspect(
        "research", playbook_id, principal_id="alice", scopes=SCOPES,
    )["reported_rehearsal_count"] == 1
    assert store.inspect(
        "research", playbook_id, principal_id="alice", scopes=SCOPES,
    )["trust_state"] == "rehearsed_reported"
    rehearsal = store.inspect(
        "research", playbook_id, principal_id="alice", scopes=SCOPES,
    )["trust_evidence"]
    assert rehearsal == {
        "run_id": run["run_id"], "run_revision": completed["revision"],
        "playbook_revision": 2, "environment": "Desktop 2.7 on laptop",
        "verified_at_ms": completed["verification"]["at_ms"],
        "observation": "New item appears after 12 seconds",
        "basis": "caller_reported_guided_rehearsal",
    }
    store.revise(
        "research", playbook_id, "edit-2", expected_revision=2,
        title="Refresh stale search safely", prerequisites=[],
        environment="Desktop 2.8", steps=STEPS,
        verification="New item appears within a minute",
        source_rationale="Updated environment after a reported rehearsal",
        principal_id="alice", scopes=SCOPES,
    )
    revised_inspection = store.inspect(
        "research", playbook_id, principal_id="alice", scopes=SCOPES,
    )
    assert revised_inspection["trust_state"] == "draft"
    assert revised_inspection["trust_evidence"] is None
    assert store.start_run(
        "research", playbook_id, "run-1", playbook_revision=2,
        environment="Desktop 2.7 on laptop", principal_id="alice", scopes=SCOPES,
    )["idempotent"] is True
    with pytest.raises(IntakeError, match="playbook changed"):
        store.start_run(
            "research", playbook_id, "run-2", playbook_revision=2,
            environment="Desktop 2.7 on laptop", principal_id="alice", scopes=SCOPES,
        )
    conn.close()


def test_playbook_source_access_is_rechecked_after_promotion(tmp_path):
    conn = duckdb.connect(str(tmp_path / "scoped.duckdb"))
    problem_id = _verified_problem(conn)
    store = IntakePlaybookStore(conn, now=lambda: 2000)
    wider = SCOPES | {"namespace:other:read"}
    created = store.promote_problem(
        "research", problem_id, "scoped-playbook", title="Scoped procedure",
        prerequisites=[], environment="Desktop 2.7", steps=STEPS,
        verification="New item appears within a minute",
        source_rationale="An external concept explains the fix",
        concept_references=[{
            "kind": "concept", "id": "concept:one", "namespace": "other", "version": 2,
        }], principal_id="alice", scopes=wider,
    )
    assert store.inspect(
        "research", created["playbook_id"], principal_id="alice", scopes=wider,
    )["references"][-1]["version"] == 2
    with pytest.raises(IntakeError, match="current access to playbook sources"):
        store.inspect(
            "research", created["playbook_id"], principal_id="alice", scopes=SCOPES,
        )
    conn.close()
