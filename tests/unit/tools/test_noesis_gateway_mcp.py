from __future__ import annotations

import asyncio
import json

from src.noesis_cli.config import initialize
from tools.noesis_mcp import server

EXPECTED_TOOLS = {
    "domains",
    "add",
    "search",
    "ask",
    "brief",
    "documents",
    "claims",
    "inspect_source",
    "coverage",
    "watch",
    "inbox",
    "explore",
    "research",
    "export",
}


def _tools():
    return asyncio.run(server.mcp.get_tools())


def test_gateway_exposes_only_curated_daily_driver_tools():
    tools = _tools()
    assert set(tools) == EXPECTED_TOOLS
    for name, tool in tools.items():
        assert tool.description and tool.description.strip(), name
        assert tool.parameters.get("type") == "object"
        assert tool.output_schema is not None


def test_gateway_workflow_tools_use_local_config(tmp_path, monkeypatch):
    config_path = tmp_path / ".noesis" / "config.json"
    initialize(config_path=config_path)
    monkeypatch.setenv("NOESIS_CONFIG", str(config_path))

    tools = _tools()

    inbox = tools["inbox"].fn(action="list", domain="local")
    assert inbox["contract"] == "noesis-intake-feed-page-v1"
    assert inbox["items"] == []

    exploration = tools["explore"].fn(
        action="start",
        domain="local",
        request_key="gateway-explore-1",
        intent="Explore the mission result",
    )
    assert exploration["mode"] == "Exploration"
    assert exploration["status"] == "active"

    research = tools["research"].fn(
        action="start",
        domain="local",
        request_key="gateway-research-1",
        question="What evidence supports the mission result?",
    )
    assert research["mode"] == "Deep Research"
    assert research["status"] == "active"

    listing = tools["research"].fn(action="list", domain="local")
    assert any(
        item["session_id"] == research["session_id"]
        for item in listing["sessions"]
    )


def test_gateway_catalog_marks_mixed_action_tools_as_writes():
    from src.mcp_host.catalog import _mutability, _required_scopes

    for name in ("add", "watch", "inbox", "explore", "research"):
        assert _mutability(name) == "write"
        assert _required_scopes("noesis_mcp", "write", name) == ["operator"]


def test_gateway_add_reports_stable_error_for_missing_source(tmp_path, monkeypatch):
    config_path = tmp_path / ".noesis" / "config.json"
    initialize(config_path=config_path)
    monkeypatch.setenv("NOESIS_CONFIG", str(config_path))

    result = _tools()["add"].fn(
        source=str(tmp_path / "missing.md"),
        domain="local",
    )

    assert result["contract"] == "noesis-gateway-v1"
    assert result["error"]["code"] == "source_not_found"
    assert "missing.md" in result["error"]["message"]
    assert "api_key" not in json.dumps(result).lower()
