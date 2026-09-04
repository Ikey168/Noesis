from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.kb.knowledge_quality import DIMENSIONS
from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def call(t, **k):
    v = t.fn(**k)
    return asyncio.run(v) if inspect.isawaitable(v) else v


def test_quality_mcp_end_to_end(tmp_path, monkeypatch):
    db = tmp_path / "quality.duckdb"
    scopes = {"knowledge:quality:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(db))
    )
    tools = asyncio.run(server.mcp.get_tools())
    names = {
        "register_quality_policy",
        "get_quality_policy",
        "assess_knowledge_quality",
        "get_quality_assessment",
        "replay_quality_assessment",
        "aggregate_quality_assessments",
        "rank_by_quality",
        "simulate_quality_policy",
        "compare_quality_policies",
        "review_quality_override",
        "inspect_quality_health",
    }
    assert names <= tools.keys()
    dims = {d: {"weight": 1, "default": None} for d in DIMENSIONS}
    assert (
        call(
            tools["register_quality_policy"],
            namespace="economic",
            policy_id="p",
            version="1",
            dimensions=dims,
        )["error"]["code"]
        == "unauthorized"
    )
    scopes.add("knowledge:quality:write")
    p = call(
        tools["register_quality_policy"],
        namespace="economic",
        policy_id="p",
        version="1",
        dimensions=dims,
    )
    assert (
        call(
            tools["assess_knowledge_quality"],
            namespace="economic",
            object_type="dataset",
            object_id="d",
            generation=1,
            policy_revision_id=p["policy_revision_id"],
            features={"coverage": 0.8},
            input_lineage=[],
        )["error"]["code"]
        == "unauthorized"
    )
    scopes.add("knowledge:quality:calculate")
    a = call(
        tools["assess_knowledge_quality"],
        namespace="economic",
        object_type="dataset",
        object_id="d",
        generation=1,
        policy_revision_id=p["policy_revision_id"],
        features={"coverage": 0.8},
        input_lineage=[{"evidence_id": "e"}],
    )
    assert call(
        tools["replay_quality_assessment"],
        namespace="economic",
        assessment_id=a["assessment_id"],
    )["deterministic"]
    assert call(
        tools["aggregate_quality_assessments"],
        namespace="economic",
        assessment_ids=[a["assessment_id"]],
    )["assessment_ids"] == [a["assessment_id"]]
    assert call(
        tools["rank_by_quality"],
        namespace="economic",
        assessment_ids=[a["assessment_id"]],
    )["low_scores_retained"]
    assert call(
        tools["inspect_quality_health"],
        namespace="economic",
        assessment_ids=[a["assessment_id"]],
    )["degraded"]
    scopes.add("knowledge:quality:review")
    assert (
        call(
            tools["review_quality_override"],
            namespace="economic",
            object_id="d",
            dimension="coverage",
            value=0.9,
            reason="human",
            reviewer_id="r",
        )["value"]
        == 0.9
    )


def test_quality_catalog():
    assert _mutability("assess_knowledge_quality") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "assess_knowledge_quality"
    ) == ["knowledge:quality:calculate"]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "simulate_quality_policy"
    ) == ["knowledge:quality:read"]
    assert (
        "noesis-quality-assessment-v1"
        in server.knowledge_engine_capabilities.fn()["contracts"]
    )
