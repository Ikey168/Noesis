"""Problem trail separates proposed fixes from reported attempts and checks."""

import duckdb
import pytest

from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_problem import IntakeProblemStore

SCOPES = {
    "knowledge:intake:read",
    "knowledge:intake:write",
    "namespace:research:read",
    "namespace:research:write",
}


def test_problem_trail_replay_restart_and_verified_completion(tmp_path):
    path = str(tmp_path / "problem.duckdb")
    conn = duckdb.connect(path)
    problem = IntakeProblemStore(conn, now=lambda: 1000)
    opened = problem.start(
        "research", "incident-1",
        symptom="Search returns stale results",
        environment="Desktop 2.7 on Linux",
        urgency="blocking today's task",
        success_check="New item appears in search within one minute",
        workspace_links=[{
            "system": "modulo", "workspace_id": "home", "kind": "task",
            "id": "task-7", "version": 1,
        }],
        principal_id="alice", scopes=SCOPES,
    )
    session_id = opened["session_id"]
    assert opened["unmet_completion_checks"]
    assert problem.start(
        "research", "incident-1",
        symptom="Search returns stale results",
        environment="Desktop 2.7 on Linux",
        urgency="blocking today's task",
        success_check="New item appears in search within one minute",
        workspace_links=[{
            "system": "modulo", "workspace_id": "home", "kind": "task",
            "id": "task-7", "version": 1,
        }],
        principal_id="alice", scopes=SCOPES,
    )["idempotent"] is True
    with pytest.raises(IntakeError, match="record_problem_step"):
        IntakeStore(conn).command(
            "research", session_id, "generic", expected_revision=1,
            action="record", payload={"data": {"verified": True, "verification": "yes"}},
            principal_id="alice", scopes=SCOPES,
        )
    proposed = problem.record_step(
        "research", session_id, "propose", expected_revision=1,
        kind="proposal", summary="Rebuild the search index",
        next_action="Check index status first",
        references=[{
            "kind": "exploration_source", "id": "source-1",
            "namespace": "research", "version": 2,
        }], principal_id="alice", scopes=SCOPES,
    )
    assert proposed["data"].get("verified") is not True
    failed = problem.record_step(
        "research", session_id, "failed-check", expected_revision=2,
        kind="verification", summary="Search for the new item",
        observation="Item did not appear", passed=False,
        next_action="Rebuild the index", principal_id="alice", scopes=SCOPES,
    )
    assert "verified_fix" in failed["unmet_completion_checks"]
    with pytest.raises(IntakeError, match="unmet completion checks"):
        IntakeStore(conn).command(
            "research", session_id, "finish-early", expected_revision=3,
            action="complete", payload=None, principal_id="alice", scopes=SCOPES,
        )
    conn.close()

    reopened = duckdb.connect(path)
    problem = IntakeProblemStore(reopened, now=lambda: 2000)
    assert problem.record_step(
        "research", session_id, "failed-check", expected_revision=2,
        kind="verification", summary="Search for the new item",
        observation="Item did not appear", passed=False,
        next_action="Rebuild the index", principal_id="alice", scopes=SCOPES,
    )["idempotent"] is True
    with pytest.raises(IntakeError, match="command_key identifies another operation"):
        problem.record_step(
            "research", session_id, "failed-check", expected_revision=2,
            kind="verification", summary="Search for the new item",
            observation="Actually it appeared", passed=True,
            next_action="Rebuild the index", principal_id="alice", scopes=SCOPES,
        )
    attempted = problem.record_step(
        "research", session_id, "attempt", expected_revision=3,
        kind="reported_attempt", summary="Rebuilt index in Modulo Desktop",
        observation="Index completed without error", principal_id="alice", scopes=SCOPES,
    )
    assert attempted["data"]["verified"] is False
    exploration = IntakeStore(reopened).create(
        "research", "Exploration", "verify-source", intent="verification source",
        principal_id="alice", scopes=SCOPES,
    )
    captured = IntakeExplorationStore(reopened).capture(
        "research", exploration["session_id"], "capture-verification-source",
        expected_revision=1, url="https://example.org/current-check",
        title="Current verification record", content="Observed the expected result.",
        principal_id="alice", scopes=SCOPES,
    )
    source_ref = captured["references"][0]
    with pytest.raises(IntakeError, match="current accessible evidence"):
        problem.record_step(
            "research", session_id, "free-text-pass", expected_revision=4,
            kind="verification", summary="Search for the new item",
            observation="New item appeared after 12 seconds", passed=True,
            principal_id="alice", scopes=SCOPES,
        )
    with pytest.raises(IntakeError, match="current readable source snapshot"):
        problem.record_step(
            "research", session_id, "stale-evidence-pass", expected_revision=4,
            kind="verification", summary="Search for the new item",
            observation="New item appeared after 12 seconds", passed=True,
            references=[{**source_ref, "version": source_ref["version"] + 1}],
            principal_id="alice", scopes=SCOPES,
        )
    checked = problem.record_step(
        "research", session_id, "passed-check", expected_revision=4,
        kind="verification", summary="Search for the new item",
        observation="New item appeared after 12 seconds", passed=True,
        references=[source_ref],
        principal_id="alice", scopes=SCOPES,
    )
    assert checked["data"]["verified"] is True
    assert len(checked["data"]["problem_trail"]) == 4
    assert checked["data"]["problem_trail"][-1]["references"] == [source_ref]
    with pytest.raises(IntakeError, match="owner"):
        IntakeStore(reopened).inspect(
            "research", session_id, principal_id="bob", scopes=SCOPES,
        )
    completed = IntakeStore(reopened).command(
        "research", session_id, "complete", expected_revision=5,
        action="complete", payload=None, principal_id="alice", scopes=SCOPES,
    )
    assert completed["status"] == "completed"
    assert completed["workspace_links"][0]["id"] == "task-7"
    reopened.close()
