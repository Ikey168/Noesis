from __future__ import annotations

import asyncio
import inspect

import duckdb

from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_hypothesis_mcp_workflow_and_access_isolation(tmp_path, monkeypatch):
    database = tmp_path / "hypotheses.duckdb"
    scopes = {"knowledge:hypothesis:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    hypotheses = [
        {
            "hypothesis_id": "h1",
            "statement": "A deployment caused the incident.",
            "predictions": [
                {
                    "statement": "A deployment immediately preceded the incident.",
                    "test": {"cost": 1},
                }
            ],
        },
        {"hypothesis_id": "h2", "statement": "Weather caused the incident."},
    ]
    denied = _call(
        tools["create_hypothesis_workspace"],
        namespace="osint",
        title="Incident",
        hypotheses=hypotheses,
    )
    assert denied["error"]["code"] == "unauthorized"

    scopes.update({"knowledge:hypothesis:write", "knowledge:hypothesis:execute"})
    workspace = _call(
        tools["create_hypothesis_workspace"],
        namespace="osint",
        title="Incident",
        hypotheses=hypotheses,
        idempotency_key="incident-1",
    )
    linked = _call(
        tools["link_hypothesis_evidence"],
        namespace="osint",
        workspace_id=workspace["workspace_id"],
        hypothesis_id="h1",
        evidence_id="change-log",
        stance="support",
        relevance=0.9,
        source_revision_id="derived-revision:change-log:1",
    )
    assert linked["stance"] == "support"
    comparison = _call(
        tools["compare_hypotheses"],
        namespace="osint",
        workspace_id=workspace["workspace_id"],
        method="weighted",
    )
    assert comparison["ranking"][0] == "h1"
    plan = _call(
        tools["create_hypothesis_research_plan"],
        namespace="osint",
        workspace_id=workspace["workspace_id"],
    )
    complete = _call(
        tools["execute_hypothesis_research_plan"],
        namespace="osint",
        plan_id=plan["plan_id"],
        observations=[{"step_id": plan["steps"][0]["step_id"], "matched": True}],
        budget=1,
    )
    assert complete["status"] == "complete"
    exported = _call(
        tools["export_hypothesis_workspace"],
        namespace="osint",
        workspace_id=workspace["workspace_id"],
    )
    assert exported["evidence"][0]["source_revision_id"].startswith("derived-revision:")
    replayed = _call(
        tools["replay_hypothesis_workspace"],
        namespace="osint",
        workspace_id=workspace["workspace_id"],
    )
    assert replayed["deterministic"] is True
    assert (
        _call(
            tools["get_hypothesis_workspace"],
            namespace="scientific",
            workspace_id=workspace["workspace_id"],
        )
        is None
    )
