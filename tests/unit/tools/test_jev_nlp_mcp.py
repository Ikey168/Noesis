from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from tools.knowledge_engine_mcp import jev_nlp


ROOT = Path(__file__).resolve().parents[3]
SOURCE_TEXT = 'The agency reported a rise. The statement was "accurate".'
SOURCE_REF = {"document_id": "report", "revision_id": "rev-1"}


class Tools:
    def __init__(self):
        self.functions = {}

    def tool(self):
        def keep(function):
            self.functions[function.__name__] = function
            return function

        return keep


class FakeDecisionRuntime:
    def __init__(self, *_args, **_kwargs):
        pass

    def capture_sources(self, _namespace, _principal, refs, _scopes):
        return (
            [{"kind": "document_revision", **ref} for ref in refs],
            [{"content": SOURCE_TEXT} for _ in refs],
        )

    def run(self, _namespace, _run_id, task, *, questions, **_kwargs):
        answers = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                answers[key] = {"kind": "noul", "status": "answered", "value": 0.9}
            elif question["type"] == "score":
                answers[key] = {"kind": "score", "status": "answered", "value": 2.0}
            else:
                choice = (
                    "uncertain"
                    if key == "speaker"
                    else "ambiguous"
                    if task.endswith("stance-v1")
                    else "mixed"
                )
                criteria = question["criteria"]
                probabilities = {
                    label: 0.8 if label == choice else 0.2 / (len(criteria) - 1)
                    for label in criteria
                }
                answers[key] = {
                    "kind": "choice",
                    "status": "answered",
                    "value": choice,
                    "probabilities": probabilities,
                    "selected_probability": 0.8,
                    "vendor_confidence": 0.5,
                }
        return {
            "status": "completed",
            "rollout_mode": "suggestion",
            "receipt": {
                "answers": answers,
                "model_requested": "jev-1.13",
                "rubric_id": "test-rubric-v1",
            },
            "source_binding": [
                {"kind": "document_revision", **SOURCE_REF}
            ],
        }


def _validate_schema(contract, output):
    schema_path = (
        ROOT
        / "contracts/schemas/jsonschema"
        / f"noesis-jev-{contract}-suggestion-v1.json"
    )
    Draft202012Validator(json.loads(schema_path.read_text())).validate(output)


def test_public_nlp_tools_have_closed_advisory_contracts(monkeypatch):
    from src.integrations.typesafe import TypeSafeClient
    from src.kb import decision_runtime

    monkeypatch.setattr(decision_runtime, "DecisionRuntime", FakeDecisionRuntime)
    monkeypatch.setattr(TypeSafeClient, "__init__", lambda self: None)
    tools = Tools()
    calls = []

    def safe(operation, *, write=False, required_scope=None):
        calls.append((write, required_scope))
        return operation(object())

    jev_nlp.register(tools, safe, lambda: ("alice", {"knowledge:decision:execute"}))
    common = {
        "namespace": "research",
        "source_ref": SOURCE_REF,
        "policy": {},
        "max_cost_usd_micros": 100,
        "network_approved": True,
    }
    claim = tools.functions["suggest_jev_claim_presence"](
        run_id="claim-run", sentence_index=0, **common
    )
    locator = {"start": 0, "end": 31}
    checkworthiness = tools.functions["suggest_jev_checkworthiness"](
        run_id="check-run",
        locator=locator,
        claim_id="claim-12",
        topic="emissions",
        **common,
    )
    sentiment = tools.functions["suggest_jev_sentiment"](
        run_id="sentiment-run", locator=locator, target="emissions", **common
    )
    attribution = tools.functions["suggest_jev_attribution"](
        run_id="attribution-run",
        statement_locator=locator,
        candidates=[{"id": "agency", "name": "Agency", "role": "institution"}],
        **common,
    )
    stance = tools.functions["suggest_jev_stance"](
        run_id="stance-run",
        topic="emissions",
        sentence_index=0,
        **common,
    )
    frames = tools.functions["suggest_jev_frames"](
        run_id="frames-run", document_kind="news", **common
    )

    for contract, output in (
        ("claim-presence", claim),
        ("checkworthiness", checkworthiness),
        ("sentiment", sentiment),
        ("attribution", attribution),
        ("stance", stance),
        ("frame", frames),
    ):
        _validate_schema(contract, output)
        assert output["accepted"] is False
    assert claim["p_claim"] == 0.9 and claim["suggested_class"] is None
    assert checkworthiness["claim_present"] is True
    assert checkworthiness["truth_status"] is None
    assert checkworthiness["scheduler_mutated"] is False
    assert sentiment["status"] == "abstained"
    assert sentiment["trend_mutated"] is False
    assert attribution["raw_choice"] == "uncertain"
    assert attribution["suggested_choice"] is None
    assert stance["candidate"] == "ambiguous"
    assert stance["prediction_status"] == "abstained"
    assert frames["coverage"]["complete"] is True
    assert frames["prediction_status"] == "abstained"
    assert calls == [(True, "knowledge:decision:execute")] * 6

    monkeypatch.setitem(globals(), "SOURCE_TEXT", "word " * 14000)
    long_frames = tools.functions["suggest_jev_frames"](
        run_id="frames-long-run", document_kind="book", **common
    )
    _validate_schema("frame", long_frames)
    assert long_frames["status"] == "abstained"
    assert long_frames["reason"] == "source_exceeds_window_limit"
