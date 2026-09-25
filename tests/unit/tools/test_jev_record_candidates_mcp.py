import json
from pathlib import Path

import duckdb
from jsonschema import Draft7Validator

from src.integrations.typesafe import TypeSafeClient
from src.kb.decision_runtime import DecisionRuntime
from src.kb.source_identity import SourceIdentityStore
from tools.knowledge_engine_mcp import jev_record_candidates


class Tools:
    def __init__(self):
        self.functions = {}

    def tool(self):
        def keep(fn):
            self.functions[fn.__name__] = fn
            return fn
        return keep


def test_public_source_candidate_then_explicit_human_alias_review(monkeypatch):
    conn = duckdb.connect(":memory:")
    sources = SourceIdentityStore(conn)
    left = sources.register("r", "publication", "Morning Post", idempotency_key="left",
                            principal_id="operator", scopes={"operator"})
    right = sources.register("r", "publication", "Morning Post Media", idempotency_key="right",
                             principal_id="operator", scopes={"operator"})
    def decide(_self, request, *, api_key, timeout_s, request_id, max_attempts,
               max_cost_micros_per_attempt, reserve_attempt):
        assert api_key == "secret"
        reserve_attempt(1, max_cost_micros_per_attempt)
        return {"contract": "noesis-typed-decision-v1", "status": "answered",
                "model_requested": request.model, "model_returned": request.model,
                "answers": {"match": {"kind": "choice", "status": "answered",
                                      "value": right["source_id"]}}}
    monkeypatch.setattr(TypeSafeClient, "decide", decide)
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret")
    DecisionRuntime(conn, client=TypeSafeClient()).configure_task_rollout(
        "r", "jev-source_matching-v1", "suggestion", model="jev-1.13.0",
        rubric_id="alias-v1", evaluation_ref="fixture-evaluation-only",
        principal_id="operator", scopes={"operator"})
    tools = Tools()
    calls = []
    def safe(fn, *, write=False, required_scope=None):
        calls.append((write, required_scope))
        return fn(conn)
    jev_record_candidates.register(tools, safe, lambda: ("operator", {"operator"}))
    suggestion = tools.functions["suggest_jev_source_identity"](
        "r", left["source_id"], [right["source_id"]], "alias-1",
        {"hosted_allowed": True, "model": "jev-1.13.0", "rubric_id": "alias-v1",
         "policy_id": "test", "credential_ref": "typesafe", "budget_id": "pilot",
         "max_total_cost_usd_micros": 1000}, 100, True)
    assert suggestion["selected_candidate_id"] == right["source_id"] and not suggestion["accepted"]
    schema = json.loads((Path(__file__).resolve().parents[3] /
                         "contracts/schemas/jsonschema/noesis-jev-record-candidate-v1.json").read_text())
    Draft7Validator(schema).validate(suggestion)
    assert conn.execute("SELECT count(*) FROM source_alias_decisions").fetchone()[0] == 0
    reviewed = tools.functions["accept_jev_source_alias_review"](
        "r", "alias-1", right["source_id"], "Human verified publisher identity")
    assert reviewed["reviewer_id"] == "operator"
    assert calls == [(True, "knowledge:decision:execute"),
                     (True, "knowledge:source-identity:review")]
    conn.close()
