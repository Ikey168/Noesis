"""Public decision comparison discovery, pinning, and current access."""

import asyncio
import json
from pathlib import Path

import duckdb
import jsonschema

from src.kb.decisions import DecisionStore
from src.mcp_host.catalog import _required_scopes
from tools.knowledge_engine_mcp import server


def test_decision_comparison_mcp(tmp_path, monkeypatch):
    path = str(tmp_path / "decision-comparison.duckdb")
    scopes = {
        "knowledge:decisions:read", "knowledge:decisions:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    with duckdb.connect(path) as conn:
        store = DecisionStore(conn)
        decision = store.create(
            "research", "renew", {
                "project": None,
                "decision_context": {
                    "question": "Renew?", "stakes": "Monthly cost",
                    "required_confidence": "moderate",
                    "stop_condition": "Current usage is known",
                    "uncertainty": "Future use is unknown",
                    "missing_inputs": ["Future use"], "deadline_at_ms": None,
                },
                "options": [{"id": "yes", "description": "Renew"},
                            {"id": "no", "description": "Cancel"}],
                "constraints": ["Budget"], "assumptions": ["Price stable"],
                "observations": [], "preferences": ["Avoid waste"],
                "selected_action": "no", "rationale": "Not used",
                "review_conditions": ["Usage returns"],
            }, principal_id="alice", scopes=scopes,
        )
        receipt = store.sensitivity(
            "research", decision["decision_id"], 1,
            weights={"value": 1},
            inputs={"yes": {"value": None}, "no": {"value": 1}},
            scenarios=[], provenance="Author-declared utilities",
            principal_id="alice", scopes=scopes,
        )
    tools = asyncio.run(server.mcp.get_tools())
    result = tools["inspect_decision_comparative_matrix"].fn(
        namespace="research", decision_id=decision["decision_id"],
        receipt_id=receipt["receipt_id"],
    )
    assert result["stop_condition"] == "Current usage is known"
    assert result["options"][0]["missing_inputs"] == ["value"]
    jsonschema.validate(
        result,
        json.loads((Path(__file__).resolve().parents[3] /
                    "contracts/schemas/jsonschema/noesis-decision-comparative-matrix-v1.json").read_text()),
    )
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "inspect_decision_comparative_matrix",
    ) == ["knowledge:decisions:read"]
    scopes.remove("knowledge:decisions:read")
    assert tools["inspect_decision_comparative_matrix"].fn(
        namespace="research", decision_id=decision["decision_id"],
        receipt_id=receipt["receipt_id"],
    )["error"]["code"] == "unauthorized"
