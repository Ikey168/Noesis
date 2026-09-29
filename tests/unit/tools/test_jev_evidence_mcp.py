"""Public MCP evidence adapters preserve bounded suggestions and provenance."""

import json
from pathlib import Path

import duckdb
from jsonschema import Draft202012Validator

from src.ingestion.revisions import DocumentRevisionStore
from src.integrations.typesafe import TypeSafeClient
from tools.knowledge_engine_mcp import jev_evidence

ROOT = Path(__file__).resolve().parents[3]
POLICY = {
    "hosted_allowed": True,
    "model": "jev-1.13.0",
    "rubric_id": "evidence-v1",
    "policy_id": "evidence-test",
    "credential_ref": "typesafe",
    "budget_id": "evidence-test",
    "max_total_cost_usd_micros": 10_000,
}


class Tools:
    def __init__(self):
        self.functions = {}

    def tool(self):
        def keep(fn):
            self.functions[fn.__name__] = fn
            return fn

        return keep


def validate(name, result):
    path = ROOT / "contracts/schemas/jsonschema" / f"{name}.json"
    Draft202012Validator(json.loads(path.read_text())).validate(result)


def test_public_rerank_support_and_relation_tools_validate_contracts(monkeypatch):
    conn = duckdb.connect(":memory:")
    revisions = DocumentRevisionStore(conn)
    passages = {
        "a": "The levy was withdrawn.",
        "b": "The levy was withdrawn.",
        "c": "The report says emissions fell by five percent.",
    }
    refs = {}
    for document_id, content in passages.items():
        revision = revisions.observe({"document_id": document_id, "content": content})
        refs[document_id] = {
            "document_id": document_id,
            "revision_id": revision["revision_id"],
        }

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
        assert api_key == "secret"
        reserve_attempt(1, max_cost_micros_per_attempt)
        answers = {}
        for key, question in request.questions.items():
            if question["type"] == "noul":
                value = 0.9 if key.endswith("_1") else 0.1
                answers[key] = {"kind": "noul", "status": "answered", "value": value}
            else:
                value = (
                    "entailment"
                    if key in {"relation", "a_to_b", "b_to_a"}
                    else "neutral"
                )
                answers[key] = {
                    "kind": "choice",
                    "status": "answered",
                    "value": value,
                    "probabilities": {value: 1.0},
                    "vendor_confidence": 0.8,
                }
        return {
            "contract": "noesis-typed-decision-v1",
            "status": "answered",
            "model_requested": request.model,
            "model_returned": request.model,
            "answers": answers,
        }

    monkeypatch.setattr(TypeSafeClient, "decide", decide)
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret")
    tools, safe_calls = Tools(), []

    def safe(fn, *, write=False, required_scope=None):
        safe_calls.append((write, required_scope))
        return fn(conn)

    jev_evidence.register(tools, safe, lambda: ("operator", {"operator"}))
    rerank = tools.functions["suggest_jev_shortlist_rerank"](
        "research",
        "rerank-public",
        "emissions change",
        [
            {
                "source_ref": refs["a"],
                "locator": {"start": 0, "end": len(passages["a"])},
                "content": passages["a"],
                "original_score": 0.8,
            },
            {
                "source_ref": refs["b"],
                "locator": {"start": 0, "end": len(passages["b"])},
                "content": passages["b"],
                "original_score": 0.2,
            },
        ],
        POLICY,
        100,
        True,
    )
    support = tools.functions["suggest_jev_answer_support"](
        "research",
        "support-public",
        "Emissions fell five percent.",
        {
            "source_ref": refs["c"],
            "locator": {"start": 0, "end": len(passages["c"]), "quote": passages["c"]},
        },
        POLICY,
        100,
        True,
    )
    relation = tools.functions["suggest_jev_claim_relation"](
        "research",
        "relation-public",
        {"source_ref": refs["a"], "locator": {"start": 0, "end": len(passages["a"])}},
        {"source_ref": refs["b"], "locator": {"start": 0, "end": len(passages["b"])}},
        0.95,
        POLICY,
        100,
        network_approved=True,
    )

    validate("noesis-jev-rerank-suggestion-v1", rerank)
    validate("noesis-jev-answer-support-suggestion-v1", support)
    validate("noesis-jev-claim-relation-suggestion-v1", relation)
    assert [row["original_index"] for row in rerank["ranking"]] == [1, 0]
    assert (
        support["relation"] == "entailment"
        and not support["counts_as_independent_source"]
    )
    assert relation["relation"] == "duplicate" and not relation["graph_write"]
    assert safe_calls == [(True, "knowledge:decision:execute")] * 3
    conn.close()
