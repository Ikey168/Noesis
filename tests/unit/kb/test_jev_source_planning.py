import duckdb
import pytest

from src.kb.decision_runtime import DecisionRuntime
from src.kb.jev_source_planning import PlanningInputResolver, suggest_source_relevance
from src.kb.source_planner import READ_SCOPE, WRITE_SCOPE, SourcePlannerError, SourcePlannerStore

AUTH = {"principal_id": "operator", "scopes": {"operator"}}
POLICY = {"hosted_allowed": True, "model": "jev-1.13.0", "rubric_id": "planning-v1",
          "policy_id": "test", "credential_ref": "secret", "budget_id": "pilot",
          "max_total_cost_usd_micros": 1_000}


class Client:
    def __init__(self):
        self.requests = []

    def decide(self, request, *, api_key, timeout_s, request_id, max_attempts,
               max_cost_micros_per_attempt, reserve_attempt):
        reserve_attempt(1, max_cost_micros_per_attempt)
        self.requests.append(request)
        return {"contract": "noesis-typed-decision-v1", "status": "answered",
                "model_requested": request.model, "model_returned": request.model,
                "answers": {key: {"kind": "score", "status": "answered", "value": 3}
                            for key in request.questions}}


def capability(store, source, *, access=None):
    return store.register_capability(
        "r", source, "1", coverage={"domains": ["r"], "evidence_classes": ["primary"]},
        authority={"score": 0.5},
        access=access or {"license_id": "open", "terms_accepted": True, "redistribution": True},
        latency={"p95_ms": 100}, cost={"per_query": 1}, rate_limits={},
        query_forms=["search"], connector={"kind": "fixture"}, dependency_group=source,
        principal_id="operator", scopes={WRITE_SCOPE}, observed_at_ms=10)


def test_model_sees_only_eligible_sources_and_score_never_bypasses_filters():
    conn = duckdb.connect(":memory:")
    planner = SourcePlannerStore(conn)
    good = capability(planner, "eligible")
    blocked = capability(planner, "blocked", access={"license_id": "restricted", "terms_accepted": False})
    objective = planner.create_objective("r", "Which source covers the result?", [], ["primary"],
                                         {"domain": "r", "allowed_licenses": ["open"], "budget": 1},
                                         principal_id="operator", scopes={WRITE_SCOPE})
    client = Client()
    runtime = DecisionRuntime(conn, client=client, input_resolver=PlanningInputResolver(conn),
                              credential_resolver=lambda _: "secret")
    result = suggest_source_relevance(runtime, planner, "r", objective["objective_id"],
                                      "planning-1", at_ms=20, allow_remote=True,
                                      policy=POLICY, max_cost_usd_micros=100, **AUTH)
    assert result["status"] == "suggested" and not result["accepted"]
    assert result["eligible_capability_ids"] == [good["capability_id"]]
    assert len(client.requests[0].state["sources"]) == 2
    assert blocked["capability_id"] not in str(client.requests[0].state)
    assert {step["source_id"] for step in result["advised_plan"]["steps"]} == {"eligible"}
    assert result["advised_plan"]["steps"][0]["score_components"]["semantic_relevance"] == 1
    assert conn.execute("SELECT count(*) FROM source_acquisition_plans").fetchone()[0] == 0
    with pytest.raises(SourcePlannerError) as gated:
        planner.preview("r", objective["objective_id"], at_ms=20, scopes={WRITE_SCOPE},
                        persist=True, semantic_scores=result["scores"])
    assert gated.value.code == "evaluation_required"
    with pytest.raises(SourcePlannerError):
        planner.preview("r", objective["objective_id"], at_ms=20, scopes={READ_SCOPE},
                        semantic_scores={good["capability_id"]: {
                            **result["scores"][good["capability_id"]],
                            "capability_content_hash": "stale"}})
    conn.close()


def test_unselected_but_budget_feasible_capability_is_scored_and_can_reorder_preview():
    conn = duckdb.connect(":memory:")
    planner = SourcePlannerStore(conn)
    capability(planner, "alpha")
    capability(planner, "beta")
    objective = planner.create_objective(
        "r", "Which source offers better evidence?", [], ["primary"],
        {"domain": "r", "budget": 1, "max_sources": 1},
        principal_id="operator", scopes={WRITE_SCOPE})
    baseline = planner.preview("r", objective["objective_id"], at_ms=20, scopes={READ_SCOPE})
    assert len(baseline["steps"]) == len(baseline["fallback_steps"]) == 1
    baseline_id = baseline["steps"][0]["capability_id"]

    class ReorderClient(Client):
        def decide(self, request, *, api_key, timeout_s, request_id, max_attempts,
                   max_cost_micros_per_attempt, reserve_attempt):
            reserve_attempt(1, max_cost_micros_per_attempt)
            self.requests.append(request)
            ids = request.state["eligible_capability_ids"]
            return {"contract": "noesis-typed-decision-v1", "status": "answered",
                    "model_requested": request.model, "model_returned": request.model,
                    "answers": {f"relevance_{index}": {
                        "kind": "score", "status": "answered", "value": 0 if cap_id == baseline_id else 3}
                        for index, cap_id in enumerate(ids)}}

    client = ReorderClient()
    runtime = DecisionRuntime(conn, client=client, input_resolver=PlanningInputResolver(conn),
                              credential_resolver=lambda _: "secret")
    result = suggest_source_relevance(runtime, planner, "r", objective["objective_id"],
                                      "planning-reorder", at_ms=20, allow_remote=True,
                                      policy=POLICY, max_cost_usd_micros=100, **AUTH)
    assert len(client.requests[0].questions) == 2
    assert len(result["scores"]) == 2
    assert result["advised_plan"]["steps"][0]["capability_id"] != baseline_id
    conn.close()


def test_jev_score_cannot_displace_required_source_and_arithmetic_stays_deterministic():
    conn = duckdb.connect(":memory:")
    planner = SourcePlannerStore(conn)
    optional = capability(planner, "aaa-optional")
    required = capability(planner, "zzz-required")
    objective = planner.create_objective(
        "r",
        "Which source provides the evidence?",
        [],
        ["primary"],
        {
            "domain": "r",
            "budget": 1,
            "max_sources": 1,
            "required_sources": ["zzz-required"],
        },
        principal_id="operator",
        scopes={WRITE_SCOPE},
    )
    baseline = planner.preview("r", objective["objective_id"], at_ms=20, scopes={READ_SCOPE})
    assert [step["source_id"] for step in baseline["steps"]] == ["zzz-required"]
    current_objective = planner.objective("r", objective["objective_id"], scopes={READ_SCOPE})
    scores = {
        optional["capability_id"]: {
            "score": 1.0,
            "objective_input_hash": current_objective["input_hash"],
            "capability_content_hash": optional["content_hash"],
            "evaluation_ref": "fixture-evaluation",
        },
        required["capability_id"]: {
            "score": 0.0,
            "objective_input_hash": current_objective["input_hash"],
            "capability_content_hash": required["content_hash"],
            "evaluation_ref": "fixture-evaluation",
        },
        "ineligible-capability": {
            "score": 1.0,
            "objective_input_hash": current_objective["input_hash"],
            "capability_content_hash": "unrelated",
            "evaluation_ref": "fixture-evaluation",
        },
    }
    advised = planner.preview(
        "r",
        objective["objective_id"],
        at_ms=20,
        scopes={READ_SCOPE},
        semantic_scores=scores,
    )
    step = advised["steps"][0]
    assert step["source_id"] == "zzz-required"
    assert advised["feasible"] is True
    assert optional["capability_id"] in {
        item["capability_id"] for item in advised["fallback_steps"]
    }
    components = step["score_components"]
    assert components["semantic_relevance"] == 0.0
    assert step["score"] == round(
        0.8 * components["baseline"] + 0.2 * components["semantic_relevance"], 8
    )
    assert conn.execute("SELECT count(*) FROM source_acquisition_plans").fetchone()[0] == 0
    conn.close()
