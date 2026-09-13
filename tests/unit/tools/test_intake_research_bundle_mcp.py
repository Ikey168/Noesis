import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_research_bundle_mcp_discovery_and_scopes(tmp_path, monkeypatch):
    path = str(tmp_path / "bundle-mcp.duckdb")
    duckdb.connect(path).close()
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "knowledge:projects:read", "knowledge:projects:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(server, "_connection",
                        lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    started = tools["start_intake_research_topic"].fn(
        namespace="research", request_key="topic", questions=["Why?"],
        success_criteria=["Explain why"],
        scope={"domains": [], "namespaces": ["research"]}, budget={"requests": 1},
    )
    document = {
        "cards": [], "claims": [], "concepts": [],
        "brief": {"text": "", "card_ids": []},
        "mental_model": {"text": "", "card_ids": []},
        "map": {"text": "", "card_ids": []},
        "known": [], "uncertain": [], "unresolved": [],
        "definition_of_done": [{"criterion": "Explain why", "met": False,
                                "rationale": "Evidence has not been gathered", "card_ids": []}],
    }
    saved = tools["save_intake_research_bundle"].fn(
        namespace="research", project_id=started["project"]["project_id"],
        command_key="draft", document=document,
    )
    assert saved["contract"] == "noesis-intake-research-bundle-v1"
    assert not saved["checks"]["ready"]
    identity = {"namespace": "research", "bundle_id": saved["bundle_id"]}
    assert tools["inspect_intake_research_bundle"].fn(**identity)["revision"] == 1
    exported = tools["export_intake_research_bundle"].fn(**identity)
    assert tools["verify_intake_research_bundle_export"].fn(exported)["valid"]
    assert _mutability("save_intake_research_bundle") == "write"
    assert _required_scopes("knowledge_engine_mcp", "write", "save_intake_research_bundle") == [
        "knowledge:intake:write", "knowledge:projects:write",
    ]
    scopes.remove("knowledge:projects:read")
    denied = tools["inspect_intake_research_bundle"].fn(**identity)
    assert denied["error"]["code"] == "unauthorized"
