"""Problem actions require current consent and a durable one-call receipt."""

import duckdb
import pytest

from src.kb.intake_modes import IntakeError
from src.kb.intake_problem import IntakeProblemStore


SCOPES = {
    "knowledge:intake:read",
    "knowledge:intake:write",
    "namespace:research:read",
    "namespace:research:write",
}


def _problem(store: IntakeProblemStore, request_key: str = "incident", *, plugin_links=None) -> dict:
    return store.start(
        "research",
        request_key,
        symptom="The worker leaves records stale",
        environment="Desktop 2.7 on Linux",
        urgency="Blocking",
        success_check="The new record appears within a minute",
        plugin_links=plugin_links,
        principal_id="alice",
        scopes=SCOPES,
    )


def _propose_and_consent(store: IntakeProblemStore, session_id: str) -> dict:
    proposed = store.propose_action(
        "research",
        session_id,
        "propose-action",
        expected_revision=1,
        action="service.restart",
        parameters={"service": "search-index", "reason": "Clear a stuck update"},
        rationale="Restart after confirming the worker is idle",
        principal_id="alice",
        scopes=SCOPES,
    )
    consent = store.consent_action(
        "research",
        session_id,
        "consent-action",
        expected_revision=proposed["revision"],
        proposal_id=proposed["data"]["problem_action_proposal"]["proposal_id"],
        consent=True,
        principal_id="alice",
        scopes=SCOPES,
    )
    return consent


def test_problem_action_is_proposed_consented_and_executed_once(tmp_path):
    path = str(tmp_path / "problem-action.duckdb")
    calls = []

    def restart(parameters, *, idempotency_key, correlation_id):
        calls.append((parameters, idempotency_key, correlation_id))
        return {"adapter_receipt_id": "service-op-42", "accepted": True}

    conn = duckdb.connect(path)
    store = IntakeProblemStore(conn, now=lambda: 1000, adapters={"service.restart": restart})
    linked_record = {
        "workspace_id": "personal", "account_id": "alice",
        "plugin_id": "evidence-reproducibility", "collection": "tasks",
        "record_id": "task-42", "authoritative_version": 3,
        "representation": "linked_projection", "authority": "modulo",
    }
    opened = _problem(store, plugin_links=[linked_record])
    session_id = opened["session_id"]
    consent = _propose_and_consent(store, session_id)
    proposal_id = consent["data"]["problem_action_proposal"]["proposal_id"]

    preview = store.preview_action(
        "research", session_id, principal_id="alice", scopes=SCOPES,
    )
    assert preview["status"] == "ready"
    assert preview["will_execute"] is False
    assert preview["session_revision"] == consent["revision"]

    executed = store.execute_action(
        "research", session_id,
        expected_revision=consent["revision"],
        idempotency_key="incident-42",
        correlation_id="trace-42",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert executed["status"] == "completed"
    assert executed["receipt"]["proposal_id"] == proposal_id
    assert executed["receipt"]["adapter_result"]["adapter_receipt_id"] == "service-op-42"
    assert executed["receipt"]["plugin_links"] == [linked_record]
    assert calls == [({"service": "search-index", "reason": "Clear a stuck update"},
                     "incident-42", "trace-42")]

    replay = store.execute_action(
        "research", session_id,
        expected_revision=consent["revision"],
        idempotency_key="incident-42",
        correlation_id="trace-42",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert replay["idempotent"] is True
    assert replay["receipt"] == executed["receipt"]
    assert len(calls) == 1
    with pytest.raises(IntakeError, match="idempotency key identifies another"):
        store.execute_action(
            "research", session_id,
            expected_revision=consent["revision"],
            idempotency_key="incident-42",
            correlation_id="trace-changed",
            principal_id="alice",
            scopes=SCOPES,
        )
    with pytest.raises(IntakeError, match="owner and matching"):
        store.execute_action(
            "research", session_id,
            expected_revision=consent["revision"],
            idempotency_key="intruder-key",
            correlation_id="trace-intruder",
            principal_id="bob",
            scopes=SCOPES,
        )
    row = conn.execute(
        "SELECT status,receipt_json FROM problem_action_executions "
        "WHERE namespace='research' AND owner='alice' AND idempotency_key='incident-42'"
    ).fetchone()
    assert row[0] == "completed"
    assert 'service-op-42' in row[1]
    conn.close()

    reopened = duckdb.connect(path)
    replay_after_restart = IntakeProblemStore(
        reopened,
        now=lambda: 2000,
        adapters={"service.restart": lambda *_args, **_kwargs: pytest.fail("must not re-dispatch")},
    ).execute_action(
        "research", session_id,
        expected_revision=consent["revision"],
        idempotency_key="incident-42",
        correlation_id="trace-42",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert replay_after_restart["idempotent"] is True
    assert replay_after_restart["receipt"]["adapter_result"]["accepted"] is True
    reopened.close()


def test_problem_action_unavailable_stale_consent_and_indeterminate_retry(tmp_path):
    conn = duckdb.connect(str(tmp_path / "problem-action-states.duckdb"))
    unavailable = IntakeProblemStore(conn, now=lambda: 1000)
    unavailable_session = _problem(unavailable, "no-adapter")
    unavailable_consent = _propose_and_consent(unavailable, unavailable_session["session_id"])
    result = unavailable.execute_action(
        "research", unavailable_session["session_id"],
        expected_revision=unavailable_consent["revision"],
        idempotency_key="unavailable-1",
        correlation_id="trace-unavailable-1",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert result == {
        "contract": "noesis-problem-action-v1",
        "status": "unavailable",
        "reason": "adapter_unavailable",
        "executed": False,
        "session_id": unavailable_session["session_id"],
        "session_revision": unavailable_consent["revision"],
        "proposal_id": unavailable_consent["data"]["problem_action_proposal"]["proposal_id"],
        "idempotency_key": "unavailable-1",
        "correlation_id": "trace-unavailable-1",
    }
    assert conn.execute("SELECT count(*) FROM problem_action_executions").fetchone()[0] == 0

    calls = []

    def flaky(_parameters, **_keys):
        calls.append(True)
        raise TimeoutError("upstream status unknown")

    configured = IntakeProblemStore(
        conn, now=lambda: 1000, adapters={"service.restart": flaky},
    )
    stale_session = _problem(configured, "stale-consent")
    consent = _propose_and_consent(configured, stale_session["session_id"])
    configured.record_step(
        "research", stale_session["session_id"], "extra-step",
        expected_revision=consent["revision"], kind="hypothesis",
        summary="Check the worker queue", principal_id="alice", scopes=SCOPES,
    )
    with pytest.raises(IntakeError, match="current explicit consent"):
        configured.execute_action(
            "research", stale_session["session_id"],
            expected_revision=consent["revision"],
            idempotency_key="stale-action",
            correlation_id="trace-stale-action",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert calls == []

    uncertain_session = _problem(configured, "uncertain-action")
    uncertain_consent = _propose_and_consent(configured, uncertain_session["session_id"])
    first = configured.execute_action(
        "research", uncertain_session["session_id"],
        expected_revision=uncertain_consent["revision"],
        idempotency_key="uncertain-key",
        correlation_id="trace-uncertain",
        principal_id="alice",
        scopes=SCOPES,
    )
    retry = configured.execute_action(
        "research", uncertain_session["session_id"],
        expected_revision=uncertain_consent["revision"],
        idempotency_key="uncertain-key",
        correlation_id="trace-uncertain",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert first["status"] == "indeterminate"
    assert retry["status"] == "indeterminate"
    assert retry["idempotent"] is True
    assert len(calls) == 1
    conn.close()


def test_problem_action_rejects_unallowlisted_or_malformed_proposal(tmp_path):
    conn = duckdb.connect(str(tmp_path / "problem-action-validation.duckdb"))
    store = IntakeProblemStore(conn)
    opened = _problem(store)
    with pytest.raises(IntakeError, match="allowlisted"):
        store.propose_action(
            "research", opened["session_id"], "bad-action", expected_revision=1,
            action="shell.exec", parameters={"cmd": "whoami"}, rationale="Inspect",
            principal_id="alice", scopes=SCOPES,
        )
    with pytest.raises(IntakeError, match="match the selected action"):
        store.propose_action(
            "research", opened["session_id"], "bad-fields", expected_revision=1,
            action="service.restart", parameters={"service": "search-index"},
            rationale="Inspect", principal_id="alice", scopes=SCOPES,
        )
    conn.close()
