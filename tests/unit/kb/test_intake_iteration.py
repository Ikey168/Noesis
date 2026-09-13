"""Measured playbook iterations retain their accepted revision and replay safety."""

import duckdb
import pytest

from src.kb.intake_iteration import IntakeIterationStore
from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_playbooks import IntakePlaybookStore
from src.kb.intake_problem import IntakeProblemStore

SCOPES = {"knowledge:intake:read", "knowledge:intake:write",
          "namespace:research:read", "namespace:research:write"}


def test_iteration_accepts_measured_playbook_revision_and_reopens_new_cycle(monkeypatch):
    conn = duckdb.connect(":memory:")
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
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
    playbooks = IntakePlaybookStore(conn, now=lambda: 1000)
    playbook = playbooks.promote_problem(
        "research", opened["session_id"], "guide", title="Repair search",
        prerequisites=[], environment="Desktop",
        steps=[{"action": "Restart worker", "expected_result": "Item visible",
                "recovery": "Inspect logs"}], verification="New item visible",
        source_rationale="Observed repair", **kwargs)
    store = IntakeIterationStore(conn, now=lambda: 2000)
    session = store.start("research", "cycle-one", playbook_id=playbook["playbook_id"],
                          expected_revision=1, expected="Search result in under 2 seconds",
                          stability_criteria="Three runs without a stale result",
                          intent="Check the repair after use", **kwargs)
    with pytest.raises(IntakeError, match="typed Iteration"):
        IntakeStore(conn, now=lambda: 2000).command(
            "research", session["session_id"], "bypass", expected_revision=1,
            action="record", payload={"data": {"learning": "fabricated"}}, **kwargs)
    observed = store.record_outcome(
        "research", session["session_id"], "observation", expected_revision=1,
        observed="Search took 5 seconds", learning="Restart alone is insufficient",
        measurements=[{"metric": "search latency", "expected": "2", "observed": "5",
                       "unit": "seconds"}], uncertainty="One run",
        external_causes="Heavy sync may contribute", evidence=[], **kwargs)
    assert store.record_outcome(
        "research", session["session_id"], "observation", expected_revision=1,
        observed="Search took 5 seconds", learning="Restart alone is insufficient",
        measurements=[{"metric": "search latency", "expected": "2", "observed": "5",
                       "unit": "seconds"}], uncertainty="One run",
        external_causes="Heavy sync may contribute", evidence=[], **kwargs)["idempotent"]
    proposed = store.propose(
        "research", session["session_id"], "proposal", expected_revision=observed["revision"],
        title="Repair search", prerequisites=[], environment="Desktop",
        steps=[{"action": "Restart and wait for sync", "expected_result": "Item visible",
                "recovery": "Inspect logs"}], verification="New item in two seconds",
        source_rationale="Observed slow search", before_after_rationale="Wait for sync after restart",
        **kwargs)
    original_command = IntakeStore.command
    interrupted = False

    def lose_response_once(self, namespace, session_id, command_key, **options):
        nonlocal interrupted
        if command_key == "accept" and not interrupted:
            interrupted = True
            raise IntakeError("interrupted", "simulate a lost result after playbook revision")
        return original_command(self, namespace, session_id, command_key, **options)

    monkeypatch.setattr(IntakeStore, "command", lose_response_once)
    with pytest.raises(IntakeError, match="simulate a lost result"):
        store.accept("research", session["session_id"], "accept",
                     expected_revision=proposed["revision"], **kwargs)
    assert playbooks.inspect("research", playbook["playbook_id"], **kwargs)["revision"] == 2
    accepted = store.accept("research", session["session_id"], "accept",
                            expected_revision=proposed["revision"], **kwargs)
    assert accepted["data"]["accepted_revision"]["revision"] == 2
    assert store.accept("research", session["session_id"], "accept",
                        expected_revision=proposed["revision"], **kwargs)["idempotent"]
    revised = playbooks.inspect("research", playbook["playbook_id"], **kwargs)
    assert revised["revision"] == 2
    assert revised["iteration_history"][0]["outcome"]["uncertainty"] == "One run"
    assert revised["iteration_history"][0]["proposal_sha256"]
    with pytest.raises(IntakeError, match="stability_review"):
        IntakeStore(conn, now=lambda: 2000).command(
            "research", session["session_id"], "too-early", expected_revision=accepted["revision"],
            action="complete", payload=None, **kwargs)
    stable = store.review_stability(
        "research", session["session_id"], "stability", expected_revision=accepted["revision"],
        stable=False, observation="One run is not enough to call stable", **kwargs)
    completed = IntakeStore(conn, now=lambda: 2000).command(
        "research", session["session_id"], "finish", expected_revision=stable["revision"],
        action="complete", payload=None, **kwargs)
    assert completed["status"] == "completed"
    next_cycle = store.start("research", "cycle-two", playbook_id=playbook["playbook_id"],
                             expected_revision=2, expected="Two-second result",
                             stability_criteria="Three clean runs", intent="New feedback",
                             origin_session_id=session["session_id"], **kwargs)
    assert next_cycle["origin"]["session_id"] == session["session_id"]
    assert store.start("research", "cycle-one", playbook_id=playbook["playbook_id"],
                       expected_revision=1, expected="Search result in under 2 seconds",
                       stability_criteria="Three runs without a stale result",
                       intent="Check the repair after use", **kwargs)["idempotent"]
