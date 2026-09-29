import json
from pathlib import Path

import duckdb
from jsonschema import Draft202012Validator

from src.ingestion.revisions import DocumentRevisionStore
from src.integrations.typesafe import TypeSafeClient
from src.kb.source_planner import SourcePlannerStore, WRITE_SCOPE
from tools.knowledge_engine_mcp import jev_revision_significance, jev_source_planning


ROOT = Path(__file__).resolve().parents[3]
POLICY = {"hosted_allowed": True, "model": "jev-1.13.0", "rubric_id": "test-v1",
          "policy_id": "test", "credential_ref": "typesafe", "budget_id": "pilot",
          "max_total_cost_usd_micros": 1_000}


class Tools:
    def __init__(self):
        self.functions = {}

    def tool(self):
        def keep(fn):
            self.functions[fn.__name__] = fn
            return fn
        return keep


def schema(name, payload):
    path = ROOT / "contracts/schemas/jsonschema" / (name + ".json")
    Draft202012Validator(json.loads(path.read_text())).validate(payload)


def test_public_revision_and_planning_tools_are_unaccepted_versioned_advice(monkeypatch):
    conn = duckdb.connect(":memory:")
    first = DocumentRevisionStore(conn).observe({"document_id": "article", "content": "The count was 10."})
    second = DocumentRevisionStore(conn).observe({"document_id": "article", "content": "The count was 20."})
    planner = SourcePlannerStore(conn)
    planner.register_capability(
        "r", "fixture", "1", coverage={"domains": ["r"], "evidence_classes": ["primary"]},
        authority={"score": 0.5}, access={"license_id": "open", "terms_accepted": True},
        latency={"p95_ms": 100}, cost={"per_query": 1}, rate_limits={},
        query_forms=["search"], connector={}, dependency_group="fixture",
        principal_id="operator", scopes={WRITE_SCOPE}, observed_at_ms=10)
    objective = planner.create_objective("r", "What changed?", [], ["primary"],
                                         {"domain": "r", "budget": 1},
                                         principal_id="operator", scopes={WRITE_SCOPE})

    def decide(_self, request, *, api_key, timeout_s, request_id, max_attempts,
               max_cost_micros_per_attempt, reserve_attempt):
        assert api_key == "secret"
        reserve_attempt(1, max_cost_micros_per_attempt)
        answers = {}
        for key in request.questions:
            if key == "claim_category":
                answers[key] = {"kind": "choice", "status": "answered", "value": "numeric_change"}
            else:
                answers[key] = {"kind": "score", "status": "answered", "value": 3}
        return {"contract": "noesis-typed-decision-v1", "status": "answered",
                "model_requested": request.model, "model_returned": request.model,
                "answers": answers}

    monkeypatch.setattr(TypeSafeClient, "decide", decide)
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret")
    tools = Tools()
    calls = []

    def safe(fn, *, write=False, required_scope=None):
        calls.append((write, required_scope))
        return fn(conn)

    jev_revision_significance.register(tools, safe, lambda: ("operator", {"operator"}))
    jev_source_planning.register(tools, safe, lambda: ("operator", {"operator"}))
    revision = tools.functions["suggest_jev_revision_significance"](
        "r", first["revision_id"], second["revision_id"], "public-revision",
        POLICY, 100, True)
    planning = tools.functions["suggest_jev_source_relevance"](
        "r", objective["objective_id"], "public-planning", 20,
        POLICY, 100, True)
    schema("noesis-jev-revision-significance-v1", revision)
    schema("noesis-jev-source-planning-v1", planning)
    assert revision["status"] == planning["status"] == "suggested"
    assert not revision["accepted"] and not planning["accepted"]
    assert calls == [(True, "knowledge:decision:execute")] * 2
    conn.close()
