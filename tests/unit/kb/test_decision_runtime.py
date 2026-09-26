import duckdb
import json
import pytest
from jsonschema import Draft7Validator
from pathlib import Path

from src.ingestion.revisions import DocumentRevisionStore
from src.kb.decision_runtime import DecisionRuntime, DecisionRuntimeError
from src.kb.intake_inbox import IntakeInboxStore
from src.integrations.typesafe import TypeSafeClient


POLICY = {
    "hosted_allowed": True,
    "model": "jev-1.13.0",
    "rubric_id": "relevance-v1",
    "policy_id": "test-hosted-v1",
    "credential_ref": "typesafe-test",
    "budget_id": "pilot",
    "max_total_cost_usd_micros": 1000,
    "max_concurrent": 1,
    "response_retention": "decision",
}
QUESTIONS = {"relevant": {"kind": "choice", "instructions": "Is this relevant?",
                           "criteria": {"yes": "Relevant", "no": "Irrelevant"}}}


class Client:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def decide(self, request, *, api_key, timeout_s, request_id, max_attempts,
               max_cost_micros_per_attempt, reserve_attempt):
        assert api_key == "secret"
        reserve_attempt(1, max_cost_micros_per_attempt)
        self.calls.append(request)
        if self.callback:
            self.callback()
        return {
            "contract": "noesis-typed-decision-v1", "status": "answered",
            "answers": {"relevant": {"kind": "choice", "status": "answered", "value": "yes"}},
            "model_requested": "jev-1.13.0", "model_returned": "jev-1.13.0",
            "raw_response": "private response text",
        }


def _setup():
    conn = duckdb.connect()
    revisions = DocumentRevisionStore(conn)
    record = revisions.observe({"document_id": "doc", "content": "Captured evidence"})
    client = Client()
    runtime = DecisionRuntime(conn, client=client, credential_resolver=lambda ref: "secret")
    args = dict(namespace="alpha", run_id="run-1", task="awareness", state={"topic": "water"},
                questions=QUESTIONS, source_refs=[{"document_id": "doc", "revision_id": record["revision_id"]}],
                principal_id="alice", scopes={"operator"}, allow_remote=True, policy=POLICY,
                max_cost_usd_micros=100, deadline_s=20)
    return conn, revisions, runtime, client, args


def test_document_execution_replay_and_access_recheck():
    conn, _, runtime, client, args = _setup()
    result = runtime.run(**args)
    assert result["status"] == "completed" and result["remote_processing_used"]
    assert result["hosted_inference_used"] and result["receipt"]["answers"]["relevant"]["value"] == "yes"
    assert result["artifact"]["kind"] == "enrichment"
    assert runtime.graph.inspect(result["artifact"]["artifact_id"])["content"]["machine_suggestion"]
    assert "raw_response" not in result["receipt"]
    assert client.calls[0].state["sources"][0]["content"] == "Captured evidence"
    assert runtime.run(**args)["replayed"] and len(client.calls) == 1
    with pytest.raises(DecisionRuntimeError) as error:
        runtime.inspect("alpha", "run-1", principal_id="alice", scopes=set())
    assert error.value.code == "unauthorized"
    with pytest.raises(DecisionRuntimeError) as error:
        runtime.run(**{**args, "state": {"topic": "changed"}})
    assert error.value.code == "run_conflict"
    with pytest.raises(DecisionRuntimeError) as error:
        runtime.inspect("beta", "run-1", principal_id="alice", scopes={"operator"})
    assert error.value.code == "run_unavailable"
    conn.close()


def test_exact_source_slice_is_bound_and_rechecked_on_inspection():
    conn, revisions, runtime, client, args = _setup()
    sliced = {**args, "source_slices": [{"start": 2, "end": 10}]}
    result = runtime.run(**sliced)
    assert client.calls[0].state["sources"][0]["content"] == "ptured e"
    assert result["source_binding"][0]["slice"]["start"] == 2
    assert runtime.inspect("alpha", "run-1", principal_id="alice", scopes={"operator"})["source_binding"] == result["source_binding"]
    with pytest.raises(DecisionRuntimeError) as invalid:
        runtime.run(**{**sliced, "run_id": "bad-slice", "source_slices": [{"start": 2, "end": 1000}]})
    assert invalid.value.code == "source_changed"
    revisions.observe({"document_id": "doc", "content": "Changed"})
    with pytest.raises(DecisionRuntimeError) as stale:
        runtime.inspect("alpha", "run-1", principal_id="alice", scopes={"operator"})
    assert stale.value.code == "source_changed"
    conn.close()


def test_stale_source_during_call_cannot_publish_success():
    conn, revisions, runtime, client, args = _setup()
    client.callback = lambda: revisions.observe({"document_id": "doc", "content": "Changed"})
    result = runtime.run(**args)
    assert result["status"] == "failed" and result["failure_code"] == "source_changed"
    assert result["artifact"] is None
    assert conn.execute("SELECT count(*) FROM knowledge_artifacts").fetchone()[0] == 0
    with pytest.raises(DecisionRuntimeError) as error:
        runtime.run(**args)
    assert error.value.code == "source_changed"
    conn.close()


def test_explicit_remote_policy_and_cost_boundaries():
    conn, _, runtime, client, args = _setup()
    for changed, code in [({"allow_remote": False}, "remote_disabled"),
                          ({"policy": {**POLICY, "hosted_allowed": False}}, "remote_disabled"),
                          ({"max_cost_usd_micros": 1001}, "invalid_budget")]:
        with pytest.raises(DecisionRuntimeError) as error:
            runtime.run(**{**args, **changed})
        assert error.value.code == code
    assert not client.calls
    assert runtime.run(**args)["status"] == "completed"
    with pytest.raises(DecisionRuntimeError) as error:
        runtime.run(**{**args, "run_id": "run-2", "max_cost_usd_micros": 950})
    assert error.value.code == "budget_exhausted"
    conn.close()


def test_inbox_uses_actual_item_version():
    conn = duckdb.connect()
    IntakeInboxStore(conn)
    conn.execute("INSERT INTO intake_inbox_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        "alpha", "alice", "feed:one", "[]", "https://example.org/item", "Headline",
        "Captured feed body", None, 1, 1, 2, "hash", None, None, None,
    ])
    conn.execute("INSERT INTO intake_inbox_item_revisions VALUES (?,?,?,?,?,?,?,?,?)", [
        "alpha", "alice", "feed:one", 2, "https://example.org/item", "Headline",
        "Captured feed body", None, 2,
    ])
    client = Client()
    runtime = DecisionRuntime(conn, client=client, credential_resolver=lambda ref: "secret")
    args = dict(namespace="alpha", run_id="inbox-1", task="awareness", state={"topic": "water"},
                questions=QUESTIONS, source_refs=[{"item_id": "feed:one", "source_version": 2}],
                principal_id="alice", scopes={"operator"}, allow_remote=True, policy=POLICY,
                max_cost_usd_micros=100)
    assert runtime.run(**args)["status"] == "completed"
    assert client.calls[0].source_binding[0]["kind"] == "inbox_item_version"
    assert client.calls[0].state["sources"][0]["content"] == "Captured feed body"
    conn.execute("UPDATE intake_inbox_items SET source_version=3 WHERE item_id='feed:one'")
    with pytest.raises(DecisionRuntimeError) as error:
        runtime.inspect("alpha", "inbox-1", principal_id="alice", scopes={"operator"})
    assert error.value.code == "source_changed"
    conn.close()


def test_real_adapter_retries_with_durable_attempt_reservations():
    conn, _, runtime, _, args = _setup()
    calls = []

    def transport(*, body, api_key, timeout_s):
        calls.append(body)
        if len(calls) == 1:
            return 429, {"Retry-After": "0"}, b"{}"
        payload = {
            "model": "jev-1.13.0", "usage": {"input_tokens": 10, "output_tokens": 3},
            "answers": {"relevant": {"type": "choice", "choice": "yes",
                                     "probabilities": {"yes": 0.8, "no": 0.2}, "confidence": 0.6}},
        }
        return 200, {}, json.dumps(payload).encode()

    runtime.client = TypeSafeClient(transport=transport, sleep=lambda _: None)
    result = runtime.run(**{**args, "max_attempts": 2})
    assert result["status"] == "completed" and result["attempts_reserved"] == 2
    assert len(calls) == 2
    assert conn.execute("SELECT count(*) FROM hosted_decision_attempts").fetchone()[0] == 2
    assert result["receipt"]["answers"]["relevant"]["selected_probability"] == 0.8
    assert result["receipt"]["answers"]["relevant"]["vendor_confidence"] == 0.6
    assert runtime.run(**{**args, "max_attempts": 2})["replayed"] and len(calls) == 2
    conn.close()


def test_reserved_run_is_indeterminate_and_never_reissued():
    conn, _, runtime, client, args = _setup()
    runtime.run(**args)
    conn.execute("UPDATE hosted_decision_runs SET status='reserved',result_json=NULL WHERE run_id='run-1'")
    with pytest.raises(DecisionRuntimeError) as error:
        runtime.run(**args)
    assert error.value.code == "indeterminate" and len(client.calls) == 1
    conn.close()


def test_receipt_without_reserved_attempt_cannot_be_published():
    conn, _, runtime, _, args = _setup()

    class UnreservedClient:
        def decide(self, request, **kwargs):
            return {
                "status": "answered", "model_requested": "jev-1.13.0",
                "model_returned": "jev-1.13.0",
                "answers": {"relevant": {"status": "answered", "value": "yes"}},
            }

    runtime.client = UnreservedClient()
    result = runtime.run(**args)
    assert result["status"] == "failed"
    assert result["failure_code"] == "unreserved_receipt"
    assert result["attempts_reserved"] == 0
    assert conn.execute("SELECT count(*) FROM knowledge_artifacts").fetchone()[0] == 0
    conn.close()


def test_task_rollout_pins_mode_model_rubric_and_evaluation_reference():
    conn, _, runtime, _, args = _setup()
    with pytest.raises(DecisionRuntimeError) as denied:
        runtime.configure_task_rollout(
            "alpha", "awareness", "suggestion", model="jev-1.13.0",
            rubric_id="relevance-v1", evaluation_ref="eval:human:one",
            principal_id="alice", scopes=set(),
        )
    assert denied.value.code == "unauthorized"
    configured = runtime.configure_task_rollout(
        "alpha", "awareness", "shadow", model="jev-1.13.0",
        rubric_id="relevance-v1", evaluation_ref=None,
        principal_id="alice", scopes={"operator"},
    )
    assert configured["mode"] == "shadow"
    schema = json.loads((Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema/noesis-decision-task-rollout-v1.json").read_text())
    assert not list(Draft7Validator(schema).iter_errors(configured))
    assert runtime.run(**args)["rollout_mode"] == "shadow"
    with pytest.raises(DecisionRuntimeError) as invalid:
        runtime.configure_task_rollout(
            "alpha", "other", "suggestion", model="jev-1.13.0",
            rubric_id="relevance-v1", evaluation_ref=None,
            principal_id="alice", scopes={"operator"},
        )
    assert invalid.value.code == "invalid_rollout"
    runtime.configure_task_rollout(
        "alpha", "disabled", "off", model=None, rubric_id=None,
        evaluation_ref=None, principal_id="alice", scopes={"operator"},
    )
    assert runtime.inspect_task_rollout(
        "alpha", "disabled", principal_id="alice", scopes={"operator"}
    )["mode"] == "off"
    with pytest.raises(DecisionRuntimeError) as disabled:
        runtime.run(**{**args, "run_id": "disabled-run", "task": "disabled"})
    assert disabled.value.code == "task_disabled"
    conn.close()


def test_revoked_document_read_and_cancellation_block_publication():
    conn, _, runtime, client, args = _setup()
    limited = {"knowledge:decision:execute", "namespace:alpha:write", "document:doc:read"}
    result = runtime.run(**{**args, "scopes": limited})
    assert result["status"] == "completed"
    with pytest.raises(DecisionRuntimeError) as error:
        runtime.run(**{**args, "scopes": limited - {"document:doc:read"}})
    assert error.value.code == "unauthorized"
    client.callback = lambda: None
    later = runtime.run(**{**args, "run_id": "cancelled", "cancelled": lambda: len(client.calls) > 1})
    assert later["status"] == "failed" and later["failure_code"] == "cancelled"
    assert later["receipt"] is None
    conn.close()


def test_inspect_works_on_read_only_connection(tmp_path):
    path = tmp_path / "decisions.duckdb"
    conn = duckdb.connect(str(path))
    revisions = DocumentRevisionStore(conn)
    record = revisions.observe({"document_id": "doc", "content": "Captured evidence"})
    runtime = DecisionRuntime(conn, client=Client(), credential_resolver=lambda _: "secret")
    runtime.run("alpha", "read-only", "awareness", state={"topic": "water"},
                questions=QUESTIONS, source_refs=[{"document_id": "doc", "revision_id": record["revision_id"]}],
                principal_id="alice", scopes={"operator"}, allow_remote=True,
                policy=POLICY, max_cost_usd_micros=100)
    conn.close()
    readonly = duckdb.connect(str(path), read_only=True)
    reader = DecisionRuntime(readonly, client=Client(), initialize=False)
    assert reader.inspect("alpha", "read-only", principal_id="alice", scopes={"operator"})["replayed"]
    readonly.close()
