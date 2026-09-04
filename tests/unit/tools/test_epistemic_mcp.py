from __future__ import annotations

import asyncio
import inspect

import duckdb

from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_epistemic_mcp_authorization_persistence_filters_and_explanation(
    tmp_path, monkeypatch
):
    database = tmp_path / "epistemic.duckdb"
    scopes = {"knowledge:epistemic:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(str(database)),
    )
    tools = asyncio.run(server.mcp.get_tools())

    denied = _call(
        tools["assess_epistemic_statement"],
        namespace="scientific",
        statement_id="claim-1",
        text="The effect was approximately 10 percent.",
        evidence=[],
    )
    assert denied["error"]["code"] == "unauthorized"

    scopes.add("knowledge:epistemic:write")
    assessed = _call(
        tools["assess_epistemic_statement"],
        namespace="scientific",
        statement_id="claim-1",
        text="The effect was approximately 10 percent.",
        evidence=[
            {
                "source_id": "paper",
                "independence_group": "study-1",
                "stance": "support",
                "reliability": 0.9,
                "methodology": 0.9,
            }
        ],
        generation=4,
        observed_at_ms=50,
    )
    assert assessed["effective_status"] == "estimate"
    assert assessed["generation"] == 4

    found = _call(
        tools["search_epistemic_assessments"],
        namespace="scientific",
        statuses=["estimate"],
    )
    assert found["facets"]["effective_status"] == {"estimate": 1}
    explanation = _call(
        tools["explain_epistemic_assessment"],
        namespace="scientific",
        statement_id="claim-1",
    )
    assert explanation["assessment_id"] == assessed["assessment_id"]
    assert explanation["limitations"]

    other_namespace = _call(
        tools["get_epistemic_assessment"],
        namespace="political",
        statement_id="claim-1",
    )
    assert other_namespace is None
