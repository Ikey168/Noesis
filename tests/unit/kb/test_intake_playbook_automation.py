"""Externalized procedure kinds and adapter-executed automated steps (#1576)."""

import duckdb
import pytest

from src.kb.intake_modes import IntakeError
from src.kb.intake_playbooks import IntakePlaybookStore
from tests.unit.kb.test_intake_playbooks import SCOPES, STEPS, _verified_problem

KW = {"principal_id": "alice", "scopes": SCOPES}
AUTOMATED = [
    STEPS[0],
    {"action": "Restart the search indexer", "expected_result": "Indexer healthy",
     "recovery": "Page on-call and roll back",
     "automation": {"action": "service.restart",
                    "parameters": {"service": "search-indexer", "reason": "stale index"}}},
]


def _promote(store, problem_id, kind, key, steps=STEPS, **fields):
    return store.promote_work(
        "research", problem_id, key, artifact_kind=kind, title=f"{kind} procedure",
        prerequisites=["Admin access"], environment="Desktop 2.7", steps=steps,
        verification="New item appears within a minute",
        source_rationale="Derived from the verified troubleshooting session", **fields, **KW,
    )


def test_completed_work_becomes_each_procedure_kind_as_a_draft():
    conn = duckdb.connect(":memory:")
    store = IntakePlaybookStore(conn, now=lambda: 2000)
    problem = _verified_problem(conn)

    checklist = _promote(store, problem, "checklist", "c")
    template = _promote(store, problem, "template", "t", template_body="Incident {{id}}: {{summary}}")
    defaults = _promote(store, problem, "default_configuration", "d", settings={"index.refresh_s": 30, "index.mode": "incremental"})
    rule = _promote(store, problem, "automation_rule", "r", steps=AUTOMATED, trigger="Index lag exceeds 5 minutes")

    assert [item["artifact_kind"] for item in (checklist, template, defaults, rule)] == [
        "checklist", "template", "default_configuration", "automation_rule"]
    assert all(item["trust_state"] == "draft" for item in (checklist, template, defaults, rule))
    assert defaults["settings"] == {"index.mode": "incremental", "index.refresh_s": 30}
    assert rule["steps"][1]["automation"]["action"] == "service.restart"
    assert template["origin"]["mode"] == "Problem-Solving"
    assert _promote(store, problem, "template", "t", template_body="Incident {{id}}: {{summary}}")["idempotent"]


@pytest.mark.parametrize(("kind", "steps", "fields"), [
    ("checklist", AUTOMATED, {}),
    ("template", STEPS, {}),
    ("default_configuration", STEPS, {"settings": {"nested": {"no": 1}}}),
    ("automation_rule", STEPS, {"trigger": "lag"}),
    ("playbook", STEPS, {"trigger": "lag"}),
    ("script", STEPS, {}),
])
def test_invalid_kind_fields_are_rejected(kind, steps, fields):
    conn = duckdb.connect(":memory:")
    store = IntakePlaybookStore(conn, now=lambda: 2000)
    with pytest.raises(IntakeError) as error:
        _promote(store, _verified_problem(conn), kind, "bad", steps=steps, **fields)
    assert error.value.code == "invalid_playbook"


def _run_to_automated_step(store, playbook):
    run = store.start_run("research", playbook["playbook_id"], "run-1",
                          playbook_revision=playbook["revision"], environment="Staging", **KW)
    return store.command_run("research", run["run_id"], "s1", expected_revision=1, action="step",
                             payload={"step_id": "step-1", "passed": True, "observation": "Stale"}, **KW)


def test_automated_step_executes_once_and_raises_trust_to_executed_verified():
    conn = duckdb.connect(":memory:")
    calls = []

    def restart(parameters, *, idempotency_key, correlation_id):
        calls.append((parameters, idempotency_key, correlation_id))
        return {"restarted": parameters["service"]}

    store = IntakePlaybookStore(conn, now=lambda: 2000, adapters={"service.restart": restart})
    rule = _promote(store, _verified_problem(conn), "automation_rule", "r", steps=AUTOMATED, trigger="lag")
    run = _run_to_automated_step(store, rule)
    with pytest.raises(IntakeError) as error:
        store.command_run("research", run["run_id"], "manual", expected_revision=run["revision"],
                          action="step", payload={"step_id": "step-2", "passed": True, "observation": "done"}, **KW)
    assert error.value.code == "automation_required"
    preview = store.preview_step_automation("research", run["run_id"], "step-2", **KW)
    assert preview["adapter_available"] and preview["is_next_step"]

    call = dict(expected_revision=run["revision"], preview_hash=preview["preview_hash"],
                idempotency_key="restart-1", correlation_id="corr-1", **KW)
    executed = store.execute_step_automation("research", run["run_id"], "step-2", **call)
    replay = store.execute_step_automation("research", run["run_id"], "step-2", **call)

    assert executed["status"] == "completed" and replay["idempotent"] is True
    assert len(calls) == 1 and calls[0][1:] == ("restart-1", "corr-1")
    step = executed["run"]["observations"][-1]
    assert step["basis"] == "adapter_execution" and step["execution_receipt"]["adapter_result"] == {"restarted": "search-indexer"}
    store.command_run("research", run["run_id"], "verify", expected_revision=executed["run"]["revision"],
                      action="verify", payload={"passed": True, "observation": "Item appeared"}, **KW)
    inspected = store.inspect("research", rule["playbook_id"], **KW)
    assert inspected["trust_state"] == "executed_verified"
    assert inspected["trust_evidence"]["environment"] == "Staging"
    assert inspected["trust_evidence"]["execution_receipts"] == [
        {"step_id": "step-2", "idempotency_key": "restart-1", "action": "service.restart"}]


def test_unconfigured_or_uncertain_adapter_never_counts_as_execution():
    conn = duckdb.connect(":memory:")
    public = IntakePlaybookStore(conn, now=lambda: 2000)
    rule = _promote(public, _verified_problem(conn), "automation_rule", "r", steps=AUTOMATED, trigger="lag")
    run = _run_to_automated_step(public, rule)
    preview = public.preview_step_automation("research", run["run_id"], "step-2", **KW)
    unavailable = public.execute_step_automation(
        "research", run["run_id"], "step-2", expected_revision=run["revision"],
        preview_hash=preview["preview_hash"], idempotency_key="k", correlation_id="c", **KW)
    assert unavailable["status"] == "unavailable" and unavailable["executed"] is False

    def flaky(parameters, *, idempotency_key, correlation_id):
        raise TimeoutError("receiver timed out")

    uncertain = IntakePlaybookStore(conn, now=lambda: 2000, adapters={"service.restart": flaky})
    # Adapter availability is part of the reviewed preview.
    preview = uncertain.preview_step_automation("research", run["run_id"], "step-2", **KW)
    result = uncertain.execute_step_automation(
        "research", run["run_id"], "step-2", expected_revision=run["revision"],
        preview_hash=preview["preview_hash"], idempotency_key="k2", correlation_id="c2", **KW)
    assert result["status"] == "indeterminate"
    assert result["run"]["observations"][-1]["passed"] is False
    assert uncertain.inspect("research", rule["playbook_id"], **KW)["trust_state"] == "draft"
    with pytest.raises(IntakeError) as error:
        uncertain.execute_step_automation(
            "research", run["run_id"], "step-2", expected_revision=run["revision"],
            preview_hash=preview["preview_hash"], idempotency_key="k3", correlation_id="c3", **KW)
    assert error.value.code == "preview_stale"
