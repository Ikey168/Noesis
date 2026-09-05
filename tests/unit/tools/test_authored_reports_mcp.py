import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_public_report_export_reopen_and_revision(tmp_path, monkeypatch):
    path = str(tmp_path / "reports.duckdb")
    monkeypatch.setattr(server, "_context", lambda: ("alice", {"operator"}))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    content = {"title": "Report", "sections": [{"id": "s", "title": "Notes", "assertions": [
        {"id": "a", "text": "An authored observation", "kind": "commentary", "dependencies": [], "citations": []}]}],
        "snapshot": {"id": "snapshot", "generations": {"r": 1}}, "bibliography": [], "limitations": ["No sourced assertions"]}
    report = tools["create_authored_report"].fn(namespace="r", request_key="report", content=content)
    identity = {"namespace": "r", "report_id": report["report_id"]}
    package = tools["export_authored_report"].fn(**identity)
    reopened = tools["reopen_authored_report"].fn(namespace="r", request_key="reopen", package=package)
    assert reopened["content"] == content
    content["title"] = "Revised"
    assert tools["revise_authored_report"].fn(**identity, expected_revision=1, content=content)["revision"] == 2
    assert tools["inspect_authored_report"].fn(**identity, revision=1)["content"]["title"] == "Report"
    for name in ("create_authored_report", "revise_authored_report", "reopen_authored_report"):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == ["knowledge:reports:write"]
    assert _required_scopes("knowledge_engine_mcp", "read", "export_authored_report") == ["knowledge:reports:read"]
