"""Fixture-only public screening tool workflow and contract validation."""

import json
from pathlib import Path

import duckdb
from jsonschema import Draft7Validator

from src.ingestion.revisions import DocumentRevisionStore
from src.integrations.typesafe import TypeSafeClient
from src.kb.systematic_reviews import SystematicReviewStore
from tools.knowledge_engine_mcp import systematic_review_decisions

ROOT = Path(__file__).resolve().parents[3]


class Tools:
    def __init__(self):
        self.functions = {}

    def tool(self):
        def keep(fn):
            self.functions[fn.__name__] = fn
            return fn
        return keep


def test_public_missing_abstract_remains_pending_and_human_screening_untouched():
    conn = duckdb.connect(":memory:")
    revision = DocumentRevisionStore(conn).observe({"document_id": "paper-1", "content": "Title only"})
    review = SystematicReviewStore(conn)
    protocol = review.create("r", "p", {
        "question": "Eligible?", "inclusion": ["Controlled study"], "exclusion": ["Editorial"],
        "databases": ["PubMed"], "search_expressions": ["study"], "date_from": "2020-01-01",
        "date_to": "2026-09-23", "reviewers": ["alice", "bob"], "fields": ["outcome"]},
        principal_id="coordinator", scopes={"operator"})
    candidate = review.add_candidate("r", protocol["protocol_id"], 1, publication_id="paper-1",
                                     source_revision=revision["revision_id"], source_namespace="r",
                                     search_run_id="search-1", study_id="study-1", title="Title only",
                                     abstract="", full_text_available=False,
                                     principal_id="coordinator", scopes={"operator"})
    tools = Tools()
    calls = []

    def safe(fn, *, write=False, required_scope=None):
        calls.append((write, required_scope))
        return fn(conn)

    systematic_review_decisions.register(tools, safe, lambda: ("coordinator", {"operator"}))
    suggestion = tools.functions["suggest_systematic_review_screening"](
        "r", candidate["candidate_id"], "title_abstract", "mcp-pending", {}, 0)
    inspected = tools.functions["inspect_systematic_review_screening_suggestion"](
        "r", candidate["candidate_id"], "title_abstract", "mcp-pending")
    assert suggestion["suggested_decision"] == inspected["suggested_decision"] == "pending"
    assert suggestion["decision_run"] is None and inspected["valid"]
    assert calls == [(True, "knowledge:decision:execute"), (False, "knowledge:reviews:read")]
    assert review.inspect_candidate("r", candidate["candidate_id"], principal_id="coordinator",
                                    scopes={"operator"})["screening"]["title_abstract"]["decisions"] == []
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-review-screening-suggestion-v1.json").read_text())
    Draft7Validator(schema).validate(suggestion)
    conn.close()


def test_public_hosted_fixture_path_returns_grounded_machine_only_suggestion(monkeypatch):
    conn = duckdb.connect(":memory:")
    revision = DocumentRevisionStore(conn).observe({"document_id": "paper-2", "content": "Controlled study with participants."})
    review = SystematicReviewStore(conn)
    protocol = review.create("r", "p2", {
        "question": "Eligible?", "inclusion": ["Controlled study"], "exclusion": ["Editorial"],
        "databases": ["PubMed"], "search_expressions": ["study"], "date_from": "2020-01-01",
        "date_to": "2026-09-23", "reviewers": ["alice", "bob"], "fields": ["outcome"]},
        principal_id="coordinator", scopes={"operator"})
    candidate = review.add_candidate("r", protocol["protocol_id"], 1, publication_id="paper-2",
                                     source_revision=revision["revision_id"], source_namespace="r",
                                     search_run_id="search-1", study_id="study-2", title="Controlled study",
                                     abstract="Controlled study with participants.", full_text_available=True,
                                     principal_id="coordinator", scopes={"operator"})
    def decide(_self, request, *, api_key, timeout_s, request_id, max_attempts,
               max_cost_micros_per_attempt, reserve_attempt):
        assert api_key == "secret"
        reserve_attempt(1, max_cost_micros_per_attempt)
        answers = {}
        for key in request.questions:
            value = ("satisfied" if key.endswith("I01") else "not_satisfied") if key.startswith("assessment_") else "abstract"
            answers[key] = {"kind": "choice", "status": "answered", "value": value}
        return {"contract": "noesis-typed-decision-v1", "status": "answered",
                "model_requested": request.model, "model_returned": request.model,
                "answers": answers}
    monkeypatch.setattr(TypeSafeClient, "decide", decide)
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret")
    tools = Tools()
    systematic_review_decisions.register(tools, lambda fn, **_: fn(conn),
                                          lambda: ("coordinator", {"operator"}))
    result = tools.functions["suggest_systematic_review_screening"](
        "r", candidate["candidate_id"], "title_abstract", "mcp-hosted",
        {"hosted_allowed": True, "model": "jev-1.13.0", "rubric_id": "screen-v1",
         "policy_id": "test", "credential_ref": "typesafe", "budget_id": "pilot",
         "max_total_cost_usd_micros": 1000}, 100, True)
    assert result["suggested_decision"] == "include" and result["machine_only"]
    located = result["criteria"][0]
    assert located["evidence_text"] == "Controlled study with participants."
    assert located["locator"]["kind"] == "document_revision"
    assert located["locator"]["revision_id"] == revision["revision_id"]
    assert located["locator"]["end"] == len(located["evidence_text"])
    assert result["source_binding"][0]["slice"]["end"] <= 2048
    assert review.inspect_candidate("r", candidate["candidate_id"], principal_id="coordinator",
                                    scopes={"operator"})["screening"]["title_abstract"]["status"] == "pending"
    conn.close()
