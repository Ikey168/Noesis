import duckdb
import pytest

from src.ingestion.revisions import DocumentRevisionStore
from src.kb.decision_runtime import DecisionRuntime, DecisionRuntimeError
from src.kb.jev_revision_significance import (
    RevisionPairResolver, RevisionSignificanceError, suggest_revision_significance,
)


POLICY = {"hosted_allowed": True, "model": "jev-1.13.0", "rubric_id": "revision-v1",
          "policy_id": "test", "credential_ref": "secret", "budget_id": "pilot",
          "max_total_cost_usd_micros": 1_000}
AUTH = {"principal_id": "editor", "scopes": {"operator"}}


class Client:
    def __init__(self):
        self.requests = []

    def decide(self, request, *, api_key, timeout_s, request_id, max_attempts,
               max_cost_micros_per_attempt, reserve_attempt):
        reserve_attempt(1, max_cost_micros_per_attempt)
        self.requests.append(request)
        return {"contract": "noesis-typed-decision-v1", "status": "answered",
                "model_requested": request.model, "model_returned": request.model,
                "answers": {"significance": {"kind": "score", "status": "answered", "value": 2},
                            "claim_category": {"kind": "choice", "status": "answered", "value": "factual_update"}}}


def test_revision_significance_binds_both_exact_versions_and_preserves_facts():
    conn = duckdb.connect(":memory:")
    store = DocumentRevisionStore(conn)
    before = store.observe({"document_id": "paper", "content": "The trial enrolled 100 adults."})
    after = store.observe({"document_id": "paper", "content": "The trial enrolled 200 adults."})
    resolver = RevisionPairResolver(conn)
    client = Client()
    runtime = DecisionRuntime(conn, client=client, input_resolver=resolver,
                              credential_resolver=lambda _: "secret")
    advice = suggest_revision_significance(runtime, resolver, "r", before["revision_id"],
                                           after["revision_id"], "revision-1",
                                           allow_remote=True, policy=POLICY,
                                           max_cost_usd_micros=100, **AUTH)
    assert advice["status"] == "suggested" and advice["semantic_score"] == 2
    assert advice["suggested_category"] == "factual_update"
    assert advice["coverage"]["complete"] and len(advice["source_binding"]) == 2
    assert not advice["accepted"] and not advice["captured_text_rewritten"]
    assert client.requests[0].state["sources"][0]["content"] == "The trial enrolled 100 adults."
    assert client.requests[0].state["sources"][1]["content"] == "The trial enrolled 200 adults."
    assert conn.execute("SELECT count(*) FROM document_revision_records").fetchone()[0] == 2
    store.observe({"document_id": "paper", "content": "The trial enrolled 300 adults."})
    with pytest.raises(DecisionRuntimeError):
        runtime.inspect("r", "revision-1", **AUTH)
    conn.close()


def test_contradictory_change_keeps_both_exact_passages_in_the_suggestion():
    conn = duckdb.connect(":memory:")
    store = DocumentRevisionStore(conn)
    before = store.observe({"document_id": "paper", "content": "The trial found no mortality benefit."})
    after = store.observe({"document_id": "paper", "content": "The trial found a mortality benefit."})
    resolver = RevisionPairResolver(conn)
    client = Client()
    runtime = DecisionRuntime(conn, client=client, input_resolver=resolver,
                              credential_resolver=lambda _: "secret")
    advice = suggest_revision_significance(runtime, resolver, "r", before["revision_id"],
                                           after["revision_id"], "contradiction",
                                           allow_remote=True, policy=POLICY,
                                           max_cost_usd_micros=100, **AUTH)
    sources = client.requests[0].state["sources"]
    assert sources[0]["content"] == "The trial found no mortality benefit."
    assert sources[1]["content"] == "The trial found a mortality benefit."
    assert advice["coverage"]["complete"]
    assert advice["classification_authoritative"] == "silent_substantive"
    assert not advice["accepted"]
    conn.close()


def test_revision_significance_rejects_wrong_order_and_handles_long_changes():
    conn = duckdb.connect(":memory:")
    store = DocumentRevisionStore(conn)
    before = store.observe({"document_id": "paper", "content": "A" * 9000})
    after = store.observe({"document_id": "paper", "content": "B" * 9000})
    resolver = RevisionPairResolver(conn)
    client = Client()
    runtime = DecisionRuntime(conn, client=client, input_resolver=resolver,
                              credential_resolver=lambda _: "secret")
    with pytest.raises(RevisionSignificanceError):
        suggest_revision_significance(runtime, resolver, "r", after["revision_id"],
                                      before["revision_id"], "bad", **AUTH)
    advice = suggest_revision_significance(runtime, resolver, "r", before["revision_id"],
                                           after["revision_id"], "long",
                                           allow_remote=True, policy=POLICY,
                                           max_cost_usd_micros=100, **AUTH)
    assert advice["status"] == "pending" and not advice["coverage"]["complete"]
    assert len(client.requests[0].state["sources"][0]["content"]) <= 8000
    conn.close()
