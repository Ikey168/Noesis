"""Hosted decision tools are discoverable and default to closed execution."""

import asyncio

import duckdb

from src.mcp_host.catalog import _required_scopes
from tools.knowledge_engine_mcp import server


def test_hosted_decision_mcp_authorization_and_rollout(tmp_path, monkeypatch):
    database = str(tmp_path / "decisions.duckdb")
    scopes = {"namespace:research:read", "namespace:research:write"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(database, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    for name in (
        "run_hosted_typed_decision", "inspect_hosted_typed_decision",
        "suggest_jev_task", "suggest_awareness_with_jev",
        "accept_awareness_jev_suggestion", "configure_jev_task_rollout",
        "inspect_jev_task_rollout",
    ):
        assert name in tools
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "run_hosted_typed_decision"
    ) == ["knowledge:decision:execute"]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "inspect_hosted_typed_decision"
    ) == ["knowledge:decision:read"]
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "configure_jev_task_rollout"
    ) == ["knowledge:decision:configure"]

    request = dict(
        namespace="research", run_id="fixture", task="fixture-task",
        state={}, questions={"decision": {"type": "noul", "instructions": "Relevant?"}},
        sources=[{"item_id": "missing", "source_version": 1}],
        policy={"hosted_allowed": True}, max_cost_usd_micros=100,
    )
    denied = tools["run_hosted_typed_decision"].fn(**request)
    assert denied["error"]["code"] == "unauthorized"

    scopes.add("knowledge:decision:execute")
    closed = tools["run_hosted_typed_decision"].fn(**request)
    assert closed["error"]["code"] == "remote_disabled"
    assert "TYPESAFE_API_KEY" not in str(closed)
    rollout_denied = tools["configure_jev_task_rollout"].fn(
        namespace="research", task="jev-stance-v1", mode="shadow",
        model="jev-1.13.0", rubric_id="stance-v1",
    )
    assert rollout_denied["error"]["code"] == "unauthorized"
    scopes.update({"knowledge:decision:configure", "knowledge:decision:read"})
    rollout = tools["configure_jev_task_rollout"].fn(
        namespace="research", task="jev-stance-v1", mode="shadow",
        model="jev-1.13.0", rubric_id="stance-v1",
    )
    assert rollout["mode"] == "shadow"
    assert tools["inspect_jev_task_rollout"].fn(
        namespace="research", task="jev-stance-v1"
    )["mode"] == "shadow"


def test_bounded_decision_evidence_mcp_uses_decision_scope_and_returns_receipt(tmp_path, monkeypatch):
    database = str(tmp_path / "bounded-evidence.duckdb")
    scopes = {"knowledge:decisions:read", "knowledge:decisions:write", "namespace:research:write"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(database, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    assert "record_decision_evidence" in tools
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "record_decision_evidence"
    ) == ["knowledge:decisions:write"]
    decision = tools["create_research_decision"].fn(
        namespace="research", request_key="utility-choice",
        content={
            "project": None,
            "decision_context": {
                "question": "Use the service?", "stakes": "Monthly cost",
                "required_confidence": "Moderate", "stop_condition": "Check current usage",
                "uncertainty": "Next month is unknown", "missing_inputs": [],
                "deadline_at_ms": None,
            },
            "evidence_budget": {
                "max_items": 1, "criteria": ["current usage"],
                "stop_condition": "Stop after one usage check",
            },
            "evidence_assessments": [],
            "options": [{"id": "yes", "description": "Use"}, {"id": "no", "description": "Do not use"}],
            "constraints": [], "assumptions": [], "observations": [],
            "preferences": [], "selected_action": "no", "rationale": "No use is recorded",
            "review_conditions": [],
        },
    )
    assert decision["contract"] == "noesis-decision-v3"
    recorded = tools["record_decision_evidence"].fn(
        namespace="research", decision_id=decision["decision_id"], expected_revision=1,
        command_key="usage-check-1",
        reference={"kind": "evidence", "id": "usage", "namespace": "research", "revision": 4},
        criterion="current usage", assessment="supports", rationale="The source reports no use.",
    )
    assert recorded["revision"] == 2
    assert recorded["evidence_receipt"]["remaining_items"] == 0
    assert tools["record_decision_evidence"].fn(
        namespace="research", decision_id=decision["decision_id"], expected_revision=1,
        command_key="usage-check-1",
        reference={"kind": "evidence", "id": "usage", "namespace": "research", "revision": 4},
        criterion="current usage", assessment="supports", rationale="The source reports no use.",
    )["idempotent"]
    scopes.remove("knowledge:decisions:write")
    denied = tools["record_decision_evidence"].fn(
        namespace="research", decision_id=decision["decision_id"], expected_revision=2,
        command_key="usage-check-2",
        reference={"kind": "evidence", "id": "usage-2", "namespace": "research", "revision": 1},
        criterion="current usage", assessment="context", rationale="An additional item.",
    )
    assert denied["error"]["code"] == "unauthorized"
