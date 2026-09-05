import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_project_public_tools_reopen_revise_archive_and_recheck_access(tmp_path, monkeypatch):
    path = str(tmp_path / "projects.duckdb")
    scopes = {"knowledge:projects:read", "knowledge:projects:write", "namespace:research:write", "domain:policy:read"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    request = dict(namespace="research", request_key="q", questions=["What changed?"], success_criteria=["Cite evidence"],
                   scope={"domains": ["policy"], "namespaces": []}, budget={"requests": 2})
    project = tools["create_research_project"].fn(**request)
    identity = dict(namespace="research", project_id=project["project_id"])
    assert tools["inspect_research_project"].fn(**identity)["questions"] == ["What changed?"]
    assert tools["create_research_project"].fn(**request)["idempotent"]
    assert tools["record_research_project_expenditure"].fn(**identity, receipt_id="run-1", costs={"requests": 1}, expected_revision=1)["spent"]["requests"] == 1
    assert tools["revise_research_project"].fn(**identity, questions=["Why?"], expected_revision=1)["error"]["code"] == "revision_conflict"
    assert tools["archive_research_project"].fn(**identity, expected_revision=2)["status"] == "archived"
    assert tools["list_research_projects"].fn(namespace="research")["projects"][0]["revision"] == 3
    scopes.remove("domain:policy:read")
    assert tools["inspect_research_project"].fn(**identity)["error"]["code"] == "unauthorized"
    for name in ("create_research_project", "revise_research_project", "archive_research_project", "record_research_project_expenditure"):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == ["knowledge:projects:write"]
    assert _required_scopes("knowledge_engine_mcp", "read", "inspect_research_project") == ["knowledge:projects:read"]
