"""Fixture-only hosted screening suggestions and independent human workflow."""

import copy
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.evaluation.systematic_review_screening import evaluate_screening_suggestions
from src.ingestion.revisions import DocumentRevisionStore
from src.kb.decision_runtime import DecisionRuntime
from src.kb.systematic_review_decisions import ScreeningDecisionStore
from src.kb.systematic_reviews import ReviewError, SystematicReviewStore

SCOPES = {"operator"}
PROTOCOL = {"question": "Does intervention help?", "inclusion": ["Controlled trial"],
            "exclusion": ["Editorial"], "databases": ["PubMed"],
            "search_expressions": ["intervention"], "date_from": "2020-01-01",
            "date_to": "2026-09-23", "reviewers": ["alice", "bob"], "fields": ["outcome"]}
POLICY = {"hosted_allowed": True, "model": "jev-1.13.0", "rubric_id": "screen-v1",
          "policy_id": "screen-policy-v1", "credential_ref": "test", "budget_id": "screen",
          "max_total_cost_usd_micros": 10_000, "response_retention": "decision"}


class Client:
    def __init__(self):
        self.calls = []

    def decide(self, request, *, api_key, timeout_s, request_id, max_attempts,
               max_cost_micros_per_attempt, reserve_attempt):
        assert api_key == "secret"
        reserve_attempt(1, max_cost_micros_per_attempt)
        self.calls.append(request)
        pointer = "abstract" if request.task_id.endswith("title-abstract-screen-v1") else "window-1"
        answers = {}
        for key in request.questions:
            if key.startswith("assessment_"):
                value = "satisfied" if key.endswith("I01") else "not_satisfied"
            else:
                value = pointer
            answers[key] = {"kind": "choice", "status": "answered", "value": value,
                            "selected_probability": 0.9, "vendor_confidence": 0.8}
        return {"contract": "noesis-typed-decision-v1", "status": "answered",
                "model_requested": request.model, "model_returned": request.model,
                "answers": answers}


def setup(text="Controlled trial of intervention. Not an editorial.", abstract="Controlled trial of intervention.",
          full_text_available=True):
    conn = duckdb.connect(":memory:")
    revisions = DocumentRevisionStore(conn)
    source = revisions.observe({"document_id": "paper1", "content": text})
    review = SystematicReviewStore(conn)
    protocol = review.create("r", "protocol", PROTOCOL, principal_id="coordinator", scopes=SCOPES)
    candidate = review.add_candidate("r", protocol["protocol_id"], 1,
        publication_id="paper1", source_revision=source["revision_id"], source_namespace="r",
        search_run_id="search-1", study_id="study-1", title="Controlled trial", abstract=abstract,
        full_text_available=full_text_available, principal_id="coordinator", scopes=SCOPES)
    client = Client()
    runtime = DecisionRuntime(conn, client=client, credential_resolver=lambda _: "secret")
    suggestions = ScreeningDecisionStore(conn, runtime)
    return conn, revisions, review, candidate, client, suggestions


def suggest(store, candidate, stage="title_abstract", run_id="suggest-1"):
    return store.suggest("r", candidate["candidate_id"], stage, run_id,
                         principal_id="coordinator", scopes=SCOPES, allow_remote=True,
                         policy=POLICY, max_cost_usd_micros=100)


def resolve_title(review, candidate):
    for reviewer in ("alice", "bob"):
        review.screen("r", candidate["candidate_id"], "title_abstract", 0, "include",
                      "Independent human screening", principal_id=reviewer, scopes=SCOPES)


def test_title_abstract_suggestion_is_pinned_and_not_a_reviewer_vote():
    conn, revisions, review, candidate, client, suggestions = setup()
    result = suggest(suggestions, candidate)
    assert result["suggested_decision"] == "include"
    assert [v["code"] for v in result["criteria"]] == ["I01", "E01"]
    assert all(v["locator"]["kind"] == "document_revision" for v in result["criteria"])
    assert all(v["locator"]["revision_id"] == candidate["source_revision"] for v in result["criteria"])
    assert result["machine_only"] and not result["human_vote_counted"]
    assert review.inspect_candidate("r", candidate["candidate_id"], principal_id="coordinator",
                                    scopes=SCOPES)["screening"]["title_abstract"]["status"] == "pending"
    assert suggest(suggestions, candidate)["replayed"] and len(client.calls) == 1
    assert suggestions.inspect("r", candidate["candidate_id"], "title_abstract", "suggest-1",
                               principal_id="coordinator", scopes=SCOPES)["valid"]
    revisions.observe({"document_id": "paper1", "content": "A changed version"})
    stale = suggestions.inspect("r", candidate["candidate_id"], "title_abstract", "suggest-1",
                                principal_id="coordinator", scopes=SCOPES)
    assert stale["status"] == "stale" and stale["suggested_decision"] is None
    conn.close()


def test_missing_abstract_and_full_text_are_pending_without_remote_call():
    conn, _, review, candidate, client, suggestions = setup(abstract="", full_text_available=False)
    title = suggest(suggestions, candidate)
    assert title["suggested_decision"] == "pending" and title["coverage"]["missing"] == ["abstract"]
    resolve_title(review, candidate)
    full = suggest(suggestions, candidate, "full_text", "full-missing")
    assert full["suggested_decision"] == "pending" and full["coverage"]["missing"] == ["full_text"]
    assert not client.calls
    assert review.inspect_candidate("r", candidate["candidate_id"], principal_id="coordinator",
                                    scopes=SCOPES)["screening"]["full_text"]["status"] == "pending"
    conn.close()


def test_full_text_requires_human_stage_and_incomplete_coverage_stays_pending():
    conn, _, review, candidate, client, suggestions = setup(text="Controlled trial. " * 4_000)
    with pytest.raises(ReviewError) as pending:
        suggest(suggestions, candidate, "full_text", "before-human")
    assert pending.value.code == "screening_pending"
    resolve_title(review, candidate)
    result = suggest(suggestions, candidate, "full_text", "long-full")
    assert result["suggested_decision"] == "pending"
    assert not result["coverage"]["complete"] and "full_text_after_limit" in result["coverage"]["missing"]
    assert not client.calls
    assert all(value["status"] == "not_reported" for value in result["criteria"])
    conn.close()


def test_title_abstract_metadata_must_be_located_in_the_pinned_revision():
    conn, _, _, candidate, client, suggestions = setup(
        text="A different document with no matching metadata.",
        abstract="A trial abstract not present in the source.",
    )
    result = suggest(suggestions, candidate)
    assert result["suggested_decision"] == "pending"
    assert "title_not_found_in_source_revision" in result["coverage"]["missing"]
    assert "abstract_not_found_in_source_revision" in result["coverage"]["missing"]
    assert all(value["status"] == "not_reported" for value in result["criteria"])
    assert not client.calls
    conn.close()


def test_protocol_amendment_invalidates_suggestion_without_changing_human_votes():
    conn, _, review, candidate, _, suggestions = setup()
    suggest(suggestions, candidate)
    amended = copy.deepcopy(PROTOCOL)
    amended["inclusion"].append("Adults")
    review.amend("r", candidate["protocol_id"], 1, amended, "Clarify", principal_id="coordinator", scopes=SCOPES)
    old = suggestions.inspect("r", candidate["candidate_id"], "title_abstract", "suggest-1",
                              principal_id="coordinator", scopes=SCOPES)
    assert old["status"] == "stale"
    with pytest.raises(ReviewError) as stale:
        suggest(suggestions, candidate, run_id="new-run")
    assert stale.value.code == "stale_protocol"
    conn.close()


def test_stage_separated_evaluation_reports_false_exclusions_and_criterion_accuracy():
    rows = [
        {"candidate_id": "a", "study_id": "s1", "stage": "title_abstract",
         "label_origin": "fixture", "source_revision": "r1", "protocol_revision": 1,
         "truth": "include", "suggested_decision": "exclude",
         "criteria_truth": {"I01": "satisfied"}, "criteria_assessed": {"I01": "not_satisfied"}},
        {"candidate_id": "b", "study_id": "s2", "stage": "title_abstract",
         "label_origin": "fixture", "source_revision": "r2", "protocol_revision": 1,
         "truth": "include", "suggested_decision": "pending",
         "criteria_truth": {"I01": "satisfied"}, "criteria_assessed": {"I01": "satisfied"}},
    ]
    with pytest.raises(ValueError, match="allow_fixture"):
        evaluate_screening_suggestions(rows, stage="title_abstract")
    report = evaluate_screening_suggestions(rows, stage="title_abstract", allow_fixture=True)
    assert report["false_exclusions"] == 1 and report["false_exclusion_rate"] == 0.5
    assert report["criterion_accuracy"] == 0.5 and report["pending_rate"] == 0.5
    assert not report["automated_exclusion_enabled"]
    assert report["label_origins"] == ["fixture"] and not report["task_ready"]
    schema = json.loads((Path(__file__).resolve().parents[3] /
                         "contracts/schemas/jsonschema/noesis-review-screening-evaluation-v1.json").read_text())
    Draft7Validator(schema).validate(report)
    with pytest.raises(ValueError):
        evaluate_screening_suggestions(rows, stage="full_text")
