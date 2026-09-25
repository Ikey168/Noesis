"""Fixture-only record-backed methodology and identity suggestions."""

import duckdb
import pytest

from src.ingestion.revisions import DocumentRevisionStore
from src.kb.decision_runtime import DecisionRuntime, DecisionRuntimeError
from src.kb.entity_history import EntityHistoryStore
from src.kb.jev_record_candidates import CandidateError, JevRecordCandidateAdvisor, RecordInputResolver
from src.kb.methodology_provenance import MethodologyStore
from src.kb.source_identity import SourceIdentityStore

POLICY = {"hosted_allowed": True, "model": "jev-1.13.0", "rubric_id": "records-v1",
          "policy_id": "records-test", "credential_ref": "secret-ref", "budget_id": "pilot",
          "max_total_cost_usd_micros": 5_000}
AUTH = {"principal_id": "operator", "scopes": {"operator"}}


class Client:
    def __init__(self):
        self.requests = []
        self.omit_answers = set()

    def decide(self, request, *, api_key, timeout_s, request_id, max_attempts,
               max_cost_micros_per_attempt, reserve_attempt):
        assert api_key == "secret"
        reserve_attempt(1, max_cost_micros_per_attempt)
        self.requests.append(request)
        answers = {}
        for name in request.questions:
            if name in self.omit_answers:
                continue
            if name == "match":
                value = request.state["candidate_ids"][0]
            elif name == "study_design":
                value = "randomized_controlled"
            elif name == "method":
                value = "experimental"
            else:
                value = "not_reported"
            answers[name] = {"kind": "choice", "status": "answered", "value": value}
        return {"contract": "noesis-typed-decision-v1", "status": "answered",
                "model_requested": request.model, "model_returned": request.model,
                "answers": answers}


def setup():
    conn = duckdb.connect(":memory:")
    DocumentRevisionStore(conn)
    SourceIdentityStore(conn)
    EntityHistoryStore(conn)
    MethodologyStore(conn)
    client = Client()
    resolver = RecordInputResolver(conn)
    runtime = DecisionRuntime(conn, client=client, credential_resolver=lambda _: "secret",
                              input_resolver=resolver)
    return conn, client, JevRecordCandidateAdvisor(conn, runtime)


def suggest_args():
    return {**AUTH, "allow_remote": True, "policy": POLICY,
            "max_cost_usd_micros": 100}


def test_one_source_candidate_is_version_bound_and_does_not_assert_independence():
    conn, client, advisor = setup()
    store = SourceIdentityStore(conn, initialize=False)
    left = store.register("r", "publication", "Morning Post", idempotency_key="left", **AUTH)
    right = store.register("r", "publication", "Morning Post UK", idempotency_key="right", **AUTH)
    result = advisor.suggest_source_identity("r", left["source_id"], [right["source_id"]],
                                             "source-match", **suggest_args())
    assert result["assessment"] == "match" and result["selected_candidate_id"] == right["source_id"]
    assert not result["accepted"] and not result["ownership_assessed"] and not result["independence_assessed"]
    assert len(result["source_binding"]) == 2
    assert conn.execute("SELECT count(*) FROM source_alias_decisions").fetchone()[0] == 0
    with pytest.raises(CandidateError) as not_evaluated:
        advisor.accept_source_alias("r", "source-match", right["source_id"],
                                    "Human verified publisher alias", **AUTH)
    assert not_evaluated.value.code == "evaluation_required"
    advisor.runtime.configure_task_rollout("r", "jev-source_matching-v1", "suggestion",
                                            model=POLICY["model"], rubric_id=POLICY["rubric_id"],
                                            evaluation_ref="fixture-evaluation-only", **AUTH)
    advisor.suggest_source_identity("r", left["source_id"], [right["source_id"]],
                                    "source-match-reviewed", **suggest_args())
    reviewed = advisor.accept_source_alias("r", "source-match-reviewed", right["source_id"],
                                           "Human verified publisher alias", **AUTH)
    assert reviewed["action"] == "link" and reviewed["reviewer_id"] == "operator"
    store.revise("r", right["source_id"], right["revision"], display_name="Morning Post Media", **AUTH)
    with pytest.raises(DecisionRuntimeError) as stale:
        advisor.runtime.inspect("r", "source-match", **AUTH)
    assert stale.value.code == "source_changed"
    conn.close()


def test_source_alias_and_localized_name_context_is_bound_and_invalidates_on_change():
    conn, client, advisor = setup()
    store = SourceIdentityStore(conn, initialize=False)
    reference = store.register(
        "r", "publication", "Morgenpost", idempotency_key="source-reference",
        names={"de": "Morgenpost", "en": "Morning Post"},
        native_ids={"domain": "example.test"}, **AUTH)
    candidate = store.register(
        "r", "publication", "Morning Post Media", idempotency_key="source-candidate",
        names={"en": "Morning Post Media"},
        native_ids={"domain": "example.test"}, **AUTH)
    store.decide_alias("r", reference["source_id"], "name", "Die Morgenpost",
                       reviewer_id="reviewer", scopes={"knowledge:source-identity:review"},
                       reason="Publisher archive records this former display name.")
    result = advisor.suggest_source_identity(
        "r", reference["source_id"], [candidate["source_id"]], "source-context",
        **suggest_args())
    request = client.requests[0]
    assert "Morning Post" in request.state["reference_identity"]
    assert "example.test" in request.as_wire()["questions"]["match"]["criteria"][candidate["source_id"]]
    captured = request.state["sources"][0]["content"]
    assert "die morgenpost" in captured
    assert result["assessment"] == "match"
    assert not result["ownership_assessed"] and not result["independence_assessed"]

    advisor.runtime.configure_task_rollout(
        "r", "jev-source_matching-v1", "suggestion", model=POLICY["model"],
        rubric_id=POLICY["rubric_id"], evaluation_ref="fixture-evaluation-only", **AUTH)
    advisor.suggest_source_identity(
        "r", reference["source_id"], [candidate["source_id"]], "source-context-review",
        **suggest_args())
    store.decide_alias("r", reference["source_id"], "name", "Die Morgenpost",
                       reviewer_id="reviewer-2", scopes={"knowledge:source-identity:review"},
                       reason="A second review records the same historical alias.", action="split")
    with pytest.raises(DecisionRuntimeError) as stale:
        advisor.accept_source_alias("r", "source-context-review", candidate["source_id"],
                                    "Human confirms this publisher name.", **AUTH)
    assert stale.value.code == "source_changed"
    assert conn.execute(
        "SELECT count(*) FROM source_alias_decisions WHERE source_id=?",
        [candidate["source_id"]]).fetchone()[0] == 0
    conn.close()


def test_entity_candidate_is_a_suggestion_not_a_merge_or_correction():
    conn, _, advisor = setup()
    entities = EntityHistoryStore(conn, initialize=False)
    entities.register_entity("r", "entity-a", aliases=["John Smith"], **AUTH)
    entities.register_entity("r", "entity-b", aliases=["John Smith"], **AUTH)
    result = advisor.suggest_entity_identity("r", "entity-a", ["entity-b"],
                                             "entity-match", **suggest_args())
    assert result["selected_candidate_id"] == "entity-b" and not result["accepted"]
    assert conn.execute("SELECT count(*) FROM entity_identity_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM entity_history_redirects").fetchone()[0] == 0
    with pytest.raises(CandidateError) as not_evaluated:
        advisor.queue_entity_merge_review("r", "entity-match", "entity-b",
                                           "Human confirmed same person", **AUTH)
    assert not_evaluated.value.code == "evaluation_required"
    advisor.runtime.configure_task_rollout("r", "jev-entity_matching-v1", "suggestion",
                                            model=POLICY["model"], rubric_id=POLICY["rubric_id"],
                                            evaluation_ref="fixture-evaluation-only", **AUTH)
    advisor.suggest_entity_identity("r", "entity-a", ["entity-b"],
                                    "entity-match-reviewed", **suggest_args())
    queued = advisor.queue_entity_merge_review("r", "entity-match-reviewed", "entity-b",
                                                "Human confirmed same person", **AUTH)
    assert queued["status"] == "pending_human_admin_review" and not queued["merge_applied"]
    assert queued["correction"]["status"] == "pending"
    assert conn.execute("SELECT count(*) FROM entity_history_redirects").fetchone()[0] == 0
    conn.close()


def test_methodology_categories_link_exact_passage_and_study_revision():
    conn, client, advisor = setup()
    text = "We randomized adults to treatment and control groups."
    revision = DocumentRevisionStore(conn).observe({"document_id": "paper-1", "content": text})
    methods = MethodologyStore(conn, initialize=False)
    study = methods.register_study("r", "study-1", "1.0.0", "Trial",
        {"type": "trial"}, {}, [], [], [], **AUTH)
    extraction = methods.extract("r", study["study_id"], "paper-1",
        [{"kind": "design", "text": text, "locator": {"passage": "p1"},
          "confidence": 1.0}], **AUTH)
    statement_id = extraction["items"][0]["statement_id"]
    result = advisor.suggest_methodology("r", statement_id, "method-1", **suggest_args())
    assert result["categories"]["study_design"] == "randomized_controlled"
    assert result["study_revision_id"] == study["study_revision_id"]
    assert result["source_locator"] == {"document_id": "paper-1", "revision_id": revision["revision_id"],
                                        "start": 0, "end": len(text)}
    assert result["categories"] == {
        "study_design": "randomized_controlled", "method": "experimental",
        "limitation": "not_reported"}
    assert result["numeric_values_generated"] is False and not result["accepted"]
    assert "sample_size" not in result and "effect_estimate" not in result and "quote" not in result
    assert client.requests[0].state["sources"][1]["content"] == text

    advisor.runtime.configure_task_rollout(
        "r", "jev-methodology-record-v1", "shadow", model=POLICY["model"],
        rubric_id=POLICY["rubric_id"], evaluation_ref=None, **AUTH)
    shadow = advisor.suggest_methodology("r", statement_id, "method-shadow", **suggest_args())
    assert shadow["status"] == "shadow"
    assert shadow["categories"] == {"study_design": None, "method": None, "limitation": None}

    advisor.runtime.configure_task_rollout(
        "r", "jev-methodology-record-v1", "suggestion", model=POLICY["model"],
        rubric_id=POLICY["rubric_id"], evaluation_ref="fixture-evaluation-only", **AUTH)
    client.omit_answers = {"method"}
    partial = advisor.suggest_methodology("r", statement_id, "method-partial", **suggest_args())
    assert partial["status"] == "unavailable"
    assert partial["categories"] == {"study_design": None, "method": None, "limitation": None}
    conn.close()
