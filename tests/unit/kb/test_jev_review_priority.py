import duckdb
import pytest

from src.ingestion.revisions import DocumentRevisionStore
from src.kb.cross_language import CrossLanguageStore
from src.kb.decision_runtime import DecisionRuntime
from src.kb.jev_review_priority import ReviewPriorityAdvisor
from src.kb.review_inbox import ReviewInboxStore
from src.kb.review_targets import ReviewTargetError

AUTH = {"principal_id": "coordinator", "scopes": {"operator"}}
POLICY = {"hosted_allowed": True, "model": "jev-1.13.0", "rubric_id": "priority-v1",
          "policy_id": "test", "credential_ref": "secret", "budget_id": "pilot",
          "max_total_cost_usd_micros": 1_000}


class Client:
    def decide(self, request, *, api_key, timeout_s, request_id, max_attempts,
               max_cost_micros_per_attempt, reserve_attempt):
        reserve_attempt(1, max_cost_micros_per_attempt)
        return {"contract": "noesis-typed-decision-v1", "status": "answered",
                "model_requested": request.model, "model_returned": request.model,
                "answers": {"impact": {"kind": "score", "status": "answered", "value": 3,
                                       "vendor_confidence": 0.99}}}


def setup():
    conn = duckdb.connect(":memory:")
    document = DocumentRevisionStore(conn).observe({"document_id": "doc", "content": "Original claim"})
    cross = CrossLanguageStore(conn)
    source = cross.record_text("r", "document", "doc", "Original claim", language="de", **AUTH)
    translation = cross.record_translation("r", source["text_id"], "en", "Translated claim",
                                           {"name": "fixture", "version": "1"}, **AUTH)
    inbox = ReviewInboxStore(conn)
    task = inbox.create("r", {"kind": "translation", "namespace": "r", "id": translation["translation_id"]},
                        sources=[{"document_id": "doc", "revision_id": document["revision_id"]}],
                        domain="research", impact=0.5, uncertainty=0.2,
                        rationale="High-impact translation in a conclusion", **AUTH)
    runtime = DecisionRuntime(conn, client=Client(), credential_resolver=lambda _: "secret")
    return conn, inbox, task, ReviewPriorityAdvisor(conn, runtime)


def suggest(advisor, task):
    return advisor.suggest("r", task["task_id"], "priority-1", allow_remote=True,
                           policy=POLICY, max_cost_usd_micros=100, **AUTH)


def test_priority_suggestion_is_separate_from_stored_priority_and_votes():
    conn, inbox, task, advisor = setup()
    result = suggest(advisor, task)
    assert result["baseline_priority"] == 1.2
    assert result["suggested_priority"] == 2.2
    assert result["machine_impact"] == 1
    assert result["vendor_confidence"] == 0.99
    assert not result["vendor_confidence_used_as_error_probability"]
    assert not result["votes_created"] and not result["resolution_created"]
    assert conn.execute("SELECT priority FROM review_inbox_tasks WHERE task_id=?", [task["task_id"]]).fetchone()[0] == 1.2
    assert conn.execute("SELECT count(*) FROM review_inbox_votes").fetchone()[0] == 0
    assert advisor.inspect("r", task["task_id"], "priority-1", **AUTH)["valid"]
    inbox.assign("r", task["task_id"], ["alice", "bob"], **AUTH)
    for who, label in (("alice", "accepted"), ("bob", "rejected")):
        inbox.submit("r", task["task_id"], task["target_revision_hash"],
                     {"decision": label}, "Independent review", 100, "human",
                     principal_id=who, scopes={"operator"})
    stale = advisor.inspect("r", task["task_id"], "priority-1", **AUTH)
    assert stale["status"] == "stale" and stale["suggested_priority"] is None
    conn.close()


def test_assigned_reviewer_cannot_request_coordinator_priority_advice():
    conn, inbox, task, advisor = setup()
    inbox.assign("r", task["task_id"], ["alice", "bob"], **AUTH)
    scopes = {
        "knowledge:inbox:read", "knowledge:decision:execute",
        "knowledge:cross-language:read", "namespace:r:read", "namespace:r:write",
        "document:doc:read",
    }
    with pytest.raises(ReviewTargetError) as denied:
        advisor.suggest("r", task["task_id"], "reviewer-priority", principal_id="alice",
                        scopes=scopes, allow_remote=True, policy=POLICY,
                        max_cost_usd_micros=100)
    assert denied.value.code == "unauthorized"
    assert conn.execute("SELECT count(*) FROM hosted_decision_runs").fetchone()[0] == 0
    conn.close()
