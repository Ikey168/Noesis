"""Public statement-kind tool verifies caller text against its source version."""

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.ingestion.revisions import DocumentRevisionStore
from src.integrations.typesafe import TypeSafeClient
from tools.knowledge_engine_mcp import jev_epistemic

ROOT = Path(__file__).resolve().parents[3]
POLICY = {
    "hosted_allowed": True,
    "model": "jev-1.13.0",
    "rubric_id": "epistemic-kind-v1",
    "policy_id": "epistemic-test",
    "credential_ref": "typesafe",
    "budget_id": "epistemic-test",
    "max_total_cost_usd_micros": 1_000,
}


class Tools:
    def __init__(self):
        self.functions = {}

    def tool(self):
        def keep(fn):
            self.functions[fn.__name__] = fn
            return fn

        return keep


def test_public_epistemic_suggestion_is_source_grounded_and_unverified(monkeypatch):
    conn = duckdb.connect(":memory:")
    revision = DocumentRevisionStore(conn).observe(
        {
            "document_id": "article",
            "content": "The filing reportedly confirms the result.",
        }
    )
    calls = []

    def decide(
        _self,
        request,
        *,
        api_key,
        timeout_s,
        request_id,
        max_attempts,
        max_cost_micros_per_attempt,
        reserve_attempt,
    ):
        calls.append(request)
        assert api_key == "secret"
        reserve_attempt(1, max_cost_micros_per_attempt)
        return {
            "contract": "noesis-typed-decision-v1",
            "status": "answered",
            "model_requested": request.model,
            "model_returned": request.model,
            "answers": {
                "kind": {
                    "kind": "choice",
                    "status": "answered",
                    "value": "report",
                    "selected_probability": 0.9,
                    "probabilities": {
                        "fact": 0.01,
                        "report": 0.9,
                        "allegation": 0.01,
                        "estimate": 0.01,
                        "forecast": 0.01,
                        "opinion": 0.01,
                        "hypothesis": 0.01,
                        "normative": 0.01,
                        "unknown": 0.03,
                    },
                }
            },
        }

    monkeypatch.setattr(TypeSafeClient, "decide", decide)
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret")
    tools = Tools()
    safe_calls = []

    def safe(fn, *, write=False, required_scope=None):
        safe_calls.append((write, required_scope))
        return fn(conn)

    jev_epistemic.register(tools, safe, lambda: ("alice", {"operator"}))
    suggest = tools.functions["suggest_jev_epistemic_kind"]
    source_ref = {"document_id": "article", "revision_id": revision["revision_id"]}
    result = suggest(
        "research",
        "claim-1",
        "The filing reportedly confirms the result.",
        "epistemic-run",
        [source_ref],
        POLICY,
        100,
        True,
    )
    schema = json.loads(
        (
            ROOT / "contracts/schemas/jsonschema/"
            "noesis-jev-epistemic-kind-suggestion-v1.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(result)
    assert result["status"] == "suggested" and result["kind"] == "report"
    assert result["truth_verified"] is False and result["accepted"] is False
    assert calls[0].state["statement_locator"] == {"start": 0, "end": 42}
    assert (
        calls[0].state["sources"][0]["content"]
        == "The filing reportedly confirms the result."
    )
    with pytest.raises(ValueError, match="verbatim"):
        suggest(
            "research",
            "claim-2",
            "Caller supplied unrelated text.",
            "epistemic-unbound",
            [source_ref],
            POLICY,
            100,
            True,
        )
    assert len(calls) == 1
    assert safe_calls == [(True, "knowledge:decision:execute")] * 2

    unretained = suggest(
        "research",
        "claim-3",
        "The filing reportedly confirms the result.",
        "epistemic-unretained",
        [source_ref],
        {**POLICY, "response_retention": "none"},
        100,
        True,
    )
    Draft202012Validator(schema).validate(unretained)
    assert unretained["status"] == "unavailable"
    assert unretained["selected_probability"] is None
    assert unretained["truth_verified"] is False
    assert len(calls) == 2
    conn.close()
