import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_decision_public_tools(tmp_path, monkeypatch):
    path = str(tmp_path / "decisions.duckdb")
    monkeypatch.setattr(server, "_context", lambda: ("alice", {"operator"}))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    project = tools["create_research_project"].fn(namespace="r", request_key="p", questions=["Which?"],
        success_criteria=["Evidence"], scope={"namespaces": ["r"], "domains": []}, budget={})
    content = {"project": {"id": project["project_id"], "namespace": "r", "revision": 1},
        "options": [{"id": "a", "description": "First"}, {"id": "b", "description": "Second"}],
        "constraints": [], "assumptions": [], "observations": [], "preferences": ["Prefer simplicity"],
        "selected_action": "a", "rationale": "Simpler", "review_conditions": []}
    decision = tools["create_research_decision"].fn(namespace="r", request_key="d", content=content)
    identity = {"namespace": "r", "decision_id": decision["decision_id"]}
    receipt = tools["calculate_decision_sensitivity"].fn(**identity, revision=1, weights={"simplicity": 1},
        inputs={"a": {"simplicity": 1}, "b": {"simplicity": 0}}, scenarios=[], provenance="Author utilities")
    assert receipt["baseline"]["ordering_with_ties"] == [["a"], ["b"]]
    content["rationale"] = "Reviewed again"
    assert tools["revise_research_decision"].fn(**identity, expected_revision=1, content=content)["revision"] == 2
    assert tools["inspect_research_decision"].fn(**identity, revision=1)["content"]["rationale"] == "Simpler"
    assert _mutability("calculate_decision_sensitivity") == "write"
    assert _required_scopes("knowledge_engine_mcp", "write", "calculate_decision_sensitivity") == ["knowledge:decisions:write", "knowledge:projects:read"]
