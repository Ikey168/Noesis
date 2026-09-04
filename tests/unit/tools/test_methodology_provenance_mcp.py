from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def _register(tools, namespace="scientific", external_id="doi:trial"):
    return _call(
        tools["register_methodology_study"],
        namespace=namespace,
        external_id=external_id,
        version="1",
        title=f"Method fixture {external_id}",
        design={"type": "randomized-trial"},
        population={"n": 50},
        interventions=[{"name": "treatment"}],
        comparators=[{"name": "control"}],
        outcomes=[{"name": "response"}],
    )


def test_methodology_mcp_end_to_end_authorization_replay_and_citation_closure(
    tmp_path, monkeypatch
):
    database = tmp_path / "methods.duckdb"
    scopes = {"knowledge:methodology:read"}
    monkeypatch.setattr(server, "_context", lambda: ("researcher", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "register_methodology_study",
        "get_methodology_study",
        "search_methodology_studies",
        "extract_methodology_statements",
        "replay_methodology_extraction",
        "assess_methodology_limitation",
        "list_methodology_limitations",
        "link_study_artifact",
        "get_study_replication_graph",
        "compare_study_methodologies",
        "explain_study_evidence_strength",
    }
    assert expected <= tools.keys()
    assert _register(tools)["error"]["code"] == "unauthorized"
    scopes.add("knowledge:methodology:write")
    first = _register(tools)
    second = _register(tools, external_id="doi:cohort")
    assert (
        _call(
            tools["search_methodology_studies"],
            namespace="scientific",
            query="fixture",
            limit=1,
        )["next_offset"]
        == 1
    )
    denied = _call(
        tools["extract_methodology_statements"],
        namespace="scientific",
        study_id=first["study_id"],
        document_id="paper:1",
        statements=[],
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.add("knowledge:methodology:extract")
    receipt = _call(
        tools["extract_methodology_statements"],
        namespace="scientific",
        study_id=first["study_id"],
        document_id="paper:1",
        statements=[
            {
                "kind": "sample",
                "text": "50 participants",
                "locator": {"page": 2},
                "confidence": 0.9,
            }
        ],
    )
    assert _call(
        tools["replay_methodology_extraction"],
        namespace="scientific",
        extraction_id=receipt["extraction_id"],
    )["deterministic"]
    denied_review = _call(
        tools["assess_methodology_limitation"],
        namespace="scientific",
        study_id=first["study_id"],
        framework="RoB2",
        dimension="selection",
        rationale="Reported",
        rating="low",
        reviewer_id="reviewer",
    )
    assert denied_review["error"]["code"] == "unauthorized"
    scopes.add("knowledge:methodology:review")
    _call(
        tools["assess_methodology_limitation"],
        namespace="scientific",
        study_id=first["study_id"],
        framework="RoB2",
        dimension="selection",
        rationale="Reported",
        rating="low",
        reviewer_id="reviewer",
        evidence_statement_ids=[receipt["items"][0]["statement_id"]],
    )
    assert _call(
        tools["list_methodology_limitations"],
        namespace="scientific",
        study_id=first["study_id"],
    )["items"]
    _call(
        tools["link_study_artifact"],
        namespace="scientific",
        study_id=first["study_id"],
        artifact_type="replication",
        artifact_id="doi:rep",
        relation="direct-replication",
        locator="doi:rep",
    )
    graph = _call(
        tools["get_study_replication_graph"],
        namespace="scientific",
        study_id=first["study_id"],
    )
    assert graph["citation_closure"]
    compared = _call(
        tools["compare_study_methodologies"],
        namespace="scientific",
        study_ids=[first["study_id"], second["study_id"]],
    )
    assert compared["comparison_hash"]
    assert (
        _call(
            tools["explain_study_evidence_strength"],
            namespace="scientific",
            study_id=first["study_id"],
        )["strength"]
        == "qualified"
    )


def test_methodology_mcp_scopes_and_capabilities():
    assert _mutability("extract_methodology_statements") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "extract_methodology_statements"
    ) == ["knowledge:methodology:extract"]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "compare_study_methodologies"
    ) == ["knowledge:methodology:read"]
    capabilities = server.knowledge_engine_capabilities.fn()
    assert "noesis-methodology-study-v1" in capabilities["contracts"]
    assert "reviewed-bias-and-applicability-assessments" in capabilities["features"]
