import duckdb
import pytest

from src.kb.decision_runtime import DecisionRuntime, DecisionRuntimeError
from src.kb.intake_modes import MODES, ROUTING_QUESTIONS
from src.kb.jev_intake_routing import IntentStore, suggest_intake_route

AUTH = {"principal_id": "alice", "scopes": {"operator"}}
POLICY = {"hosted_allowed": True, "model": "jev-1.13.0", "rubric_id": "intake-v1",
          "policy_id": "test", "credential_ref": "secret", "budget_id": "pilot",
          "max_total_cost_usd_micros": 10_000}


class Client:
    def __init__(self):
        self.key = None
        self.calls = 0
        self.values = None
        self.abstained = set()

    def decide(self, request, *, api_key, timeout_s, request_id, max_attempts,
               max_cost_micros_per_attempt, reserve_attempt):
        reserve_attempt(1, max_cost_micros_per_attempt)
        self.calls += 1
        return {"contract": "noesis-typed-decision-v1", "status": "answered",
                "model_requested": request.model, "model_returned": request.model,
                "answers": {
                    name: ({"kind": "noul", "status": "abstained", "reason_code": "ambiguous"}
                           if name in self.abstained else
                           {"kind": "noul", "status": "answered", "value":
                            self.values.get(name, False) if self.values is not None else name == self.key})
                    for name in request.questions
                }}


def setup():
    conn = duckdb.connect(":memory:")
    intents = IntentStore(conn)
    client = Client()
    runtime = DecisionRuntime(conn, client=client, credential_resolver=lambda _: "secret",
                              input_resolver=intents.resolve)
    return conn, intents, client, runtime


def test_explicit_answers_and_override_never_use_hosted_inference():
    conn, intents, client, runtime = setup()
    explicit = suggest_intake_route(runtime, intents, "r", "manual", answers={"decision_needed": True}, **AUTH)
    assert explicit["route"]["mode"] == "Decision Support" and not explicit["hosted_inference_used"]
    override = suggest_intake_route(runtime, intents, "r", "override", answers={"decision_needed": True},
                                    override="Creation", **AUTH)
    assert override["route"]["mode"] == "Creation" and override["route"]["overridden"]
    assert client.calls == 0
    conn.close()


@pytest.mark.parametrize("key,mode", ROUTING_QUESTIONS)
def test_free_text_routes_each_supported_mode_through_existing_precedence(key, mode):
    conn, intents, client, runtime = setup()
    intent = intents.register("r", "intent", f"I need help with {mode.lower()}.", **AUTH)
    client.key = key
    result = suggest_intake_route(runtime, intents, "r", "route-1",
                                  input_id=intent["input_id"], allow_remote=True,
                                  policy=POLICY, max_cost_usd_micros=100, **AUTH)
    assert result["status"] == "suggested" and not result["accepted"]
    assert result["route"]["mode"] == mode
    assert result["decision_run"]["source_binding"][0]["version"] == "1"
    assert client.calls == 1
    conn.close()


def test_changed_intent_invalidates_source_bound_run():
    conn, intents, client, runtime = setup()
    intent = intents.register("r", "intent", "I need to decide now.", **AUTH)
    client.key = "decision_needed"
    suggest_intake_route(runtime, intents, "r", "route-1", input_id=intent["input_id"],
                         allow_remote=True, policy=POLICY, max_cost_usd_micros=100, **AUTH)
    intents.revise("r", intent["input_id"], 1, "I need to create something.", **AUTH)
    with pytest.raises(DecisionRuntimeError) as stale:
        runtime.inspect("r", "route-1", **AUTH)
    assert stale.value.code == "source_changed"
    conn.close()


def test_mixed_intents_use_existing_route_precedence():
    from src.kb.intake_modes import route_mode

    conn, intents, client, runtime = setup()
    intent = intents.register("r", "mixed", "I need to decide and create something.", **AUTH)
    client.values = {"decision_needed": True, "creating": True}
    result = suggest_intake_route(
        runtime, intents, "r", "mixed-route", input_id=intent["input_id"],
        allow_remote=True, policy=POLICY, max_cost_usd_micros=100, **AUTH
    )
    assert result["status"] == "suggested"
    assert result["answers"]["decision_needed"] is True
    assert result["answers"]["creating"] is True
    assert result["route"] == route_mode(result["answers"])
    assert result["route"]["mode"] in MODES
    conn.close()


def test_ambiguous_free_text_abstains_without_defaulting_to_a_mode():
    conn, intents, client, runtime = setup()
    intent = intents.register("r", "ambiguous", "Help me with this.", **AUTH)
    client.abstained = set(ROUTING_QUESTIONS[i][0] for i in range(len(ROUTING_QUESTIONS)))
    result = suggest_intake_route(
        runtime, intents, "r", "ambiguous-route", input_id=intent["input_id"],
        allow_remote=True, policy=POLICY, max_cost_usd_micros=100, **AUTH
    )
    assert result["status"] == "pending"
    assert result["route"] is None and not result["accepted"]
    conn.close()
