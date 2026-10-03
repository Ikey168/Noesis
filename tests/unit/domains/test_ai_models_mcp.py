"""AI models MCP entry points: catalog registration, declared scopes, exclusions and minimisation checks (AI11, #2742)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.ai_models_records import excluded_paths, personal_data_paths
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import ai_models_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.ai_models import (
    AI_MODELS_SCOPES,
    AI_MODELS_TOOLS,
    AI_MODELS_WRITES,
    _declared,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "ai-models-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_with_every_scope_they_declare(mcp_env):
    tools, _ = mcp_env
    assert AI_MODELS_TOOLS <= set(tools) and set(AI_MODELS_SCOPES) == AI_MODELS_TOOLS
    for name in AI_MODELS_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in AI_MODELS_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == AI_MODELS_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in AI_MODELS_TOOLS:
        assert by_name[name]["required_scopes"] == AI_MODELS_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/technology/providers/technology.ai-models.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in AI_MODELS_TOOLS and name not in AI_MODELS_WRITES
        assert op["required_scopes"] == AI_MODELS_SCOPES[name]
    for name in ("ai_model_records_as_of", "ai_model_revision_history", "ai_models_source_contracts",
                 "export_ai_model_evidence", "link_ai_model_records"):
        description = " ".join(tools[name].description.lower().split())
        assert "no licence-compliance interpretation" in description and "no rankings" in description, name


def test_reads_work_with_exactly_the_declared_scopes_and_answers_pass_the_minimisation_check(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    answer = tools["ai_model_records_as_of"].fn(namespace="global", subject=h.MODEL, as_of="2096-06-01")
    assert answer["status"] == "records" and answer["side_by_side"][0]["revision"]["sha"] == h.SHAS["B"]
    assert personal_data_paths(answer) == [] and excluded_paths(answer) == []
    assert "licence-compliance interpretation" in answer["exclusions"]
    history = tools["ai_model_revision_history"].fn(namespace="global", subject=h.MODEL)
    assert len(history["revisions"]) == 3 and len(history["licence_changes"]) == 2
    bundle = tools["export_ai_model_evidence"].fn(namespace="global", subject=h.MODEL, as_of="2096-06-01")
    assert bundle["contract"] == "noesis-evidence-bundle-v1"
    records = tools["list_ai_model_records"].fn(namespace="global", source="openml")["records"]
    assert {r["record_kind"] for r in records} == {"openml-dataset", "openml-task"}
    assert tools["list_ai_model_identity_matches"].fn(namespace="global")["matches"] == []
    refused = tools["propose_ai_model_identity_matches"].fn(namespace="global")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:technical:ai-models:write", "namespace:global:write",
                       "knowledge:technical:ai-models:read"}
    proposed = tools["propose_ai_model_identity_matches"].fn(namespace="global")
    assert len(proposed["created"]) == 3
    linked = tools["link_ai_model_records"].fn(namespace="global")
    assert linked["linked"] == [] and linked["provider_absent"]
    state["scopes"] = set()
    contracts = tools["ai_models_source_contracts"].fn()
    assert contracts["live_verification"]["huggingface-hub"]["status"] == "unverified-live"
    assert contracts["minimisation"]["decision"].startswith("organisation-level registry metadata only")


def test_every_answer_is_checked_against_the_minimisation_decision():
    from src.kb.ai_models_records import AiModelsError

    with pytest.raises(AiModelsError):
        _declared({"records": [{"author": "Ada Example"}]})
    with pytest.raises(AiModelsError):
        _declared({"records": [{"downloads": 5}]})
    assert _declared({"records": []})["exclusions"]
