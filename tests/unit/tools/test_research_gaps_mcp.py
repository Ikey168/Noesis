from __future__ import annotations

import asyncio
import inspect

import duckdb

from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_research_gaps_mcp_discovery_drilldown_planning_lifecycle_and_auth(
    tmp_path, monkeypatch
):
    database = tmp_path / "research-gaps.duckdb"
    scopes = {"knowledge:gaps:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "register_research_gap_policy",
        "record_research_coverage",
        "discover_research_gaps",
        "get_research_gap",
        "explain_research_gap",
        "list_research_gaps",
        "update_research_gap_status",
        "prioritize_research_gaps",
        "list_research_gap_tasks",
        "compare_research_gap_coverage",
        "replay_research_gap",
    }
    assert expected <= tools.keys()
    denied = _call(
        tools["register_research_gap_policy"],
        namespace="economic",
        semantic_version="1",
        thresholds={},
        weights={},
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.add("knowledge:gaps:write")
    policy = _call(
        tools["register_research_gap_policy"],
        namespace="economic",
        semantic_version="1",
        thresholds={"min_independent": 2},
        weights={},
        observed_at_ms=10,
    )
    coverage = _call(
        tools["record_research_coverage"],
        namespace="economic",
        object_kind="claim",
        object_id="claim:gdp",
        dimension={"geography": "DE", "source_class": "official"},
        coverage_known=True,
        supports=[
            {
                "evidence_id": "evidence:gdp",
                "source_id": "source:statistics-office",
                "primary": True,
                "accessible": True,
                "independence_group": "statistics-office",
                "current": True,
                "method_adequate": True,
            }
        ],
        signals={"decision_relevance": 1, "estimated_cost": 1},
        observed_at_ms=20,
    )
    assert coverage["object_id"] == "claim:gdp"
    report = _call(tools["discover_research_gaps"], namespace="economic", limit=10)
    gap = next(
        item
        for item in report["items"]
        if item["gap_type"] == "insufficient-independent-support"
    )
    assert (
        _call(tools["get_research_gap"], namespace="economic", gap_id=gap["gap_id"])[
            "gap_revision_id"
        ]
        == gap["gap_revision_id"]
    )
    explanation = _call(
        tools["explain_research_gap"], namespace="economic", gap_id=gap["gap_id"]
    )
    assert explanation["explanation"]["silently_inferred"] is False
    listed = _call(tools["list_research_gaps"], namespace="economic", limit=1)
    assert len(listed["items"]) == 1
    plan = _call(
        tools["prioritize_research_gaps"], namespace="economic", budget=1, max_tasks=1
    )
    assert plan["spent"] <= plan["budget"] and len(plan["tasks"]) == 1
    tasks = _call(tools["list_research_gap_tasks"], namespace="economic")
    assert tasks["items"][0]["task_id"] == plan["tasks"][0]["task_id"]
    assert _call(
        tools["replay_research_gap"], namespace="economic", gap_id=gap["gap_id"]
    )["deterministic"]

    denied_review = _call(
        tools["update_research_gap_status"],
        namespace="economic",
        gap_id=gap["gap_id"],
        status="resolved",
        reason="new source acquired",
        evidence=[{"evidence_id": "evidence:new"}],
    )
    assert denied_review["error"]["code"] == "unauthorized"
    scopes.add("knowledge:gaps:review")
    resolved = _call(
        tools["update_research_gap_status"],
        namespace="economic",
        gap_id=gap["gap_id"],
        status="resolved",
        reason="new source acquired",
        evidence=[{"evidence_id": "evidence:new"}],
    )
    comparison = _call(
        tools["compare_research_gap_coverage"],
        namespace="economic",
        before_observed_ms=20,
        after_observed_ms=resolved["observed_at_ms"],
    )
    assert comparison["resolved_delta"] == 1
    assert policy["status"] == "active"


def test_research_gap_capabilities_advertise_contracts_and_features():
    capabilities = server.knowledge_engine_capabilities.fn()
    assert {
        "noesis-research-gap-policy-v1",
        "noesis-research-coverage-v1",
        "noesis-research-gap-v1",
        "noesis-research-gap-task-v1",
        "noesis-research-gap-report-v1",
    } <= set(capabilities["contracts"])
    assert {
        "multidimensional-research-gap-records",
        "weak-support-and-citation-chain-detection",
        "deterministic-budgeted-research-planning",
        "research-gap-lifecycle-tracking",
    } <= set(capabilities["features"])
