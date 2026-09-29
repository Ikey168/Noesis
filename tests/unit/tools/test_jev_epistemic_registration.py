from __future__ import annotations

import asyncio

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_jev_epistemic_suggestion_is_public_and_scoped():
    name = "suggest_jev_epistemic_kind"
    tools = asyncio.run(server.mcp.get_tools())
    assert name in tools
    assert _mutability(name) == "write"
    assert _required_scopes("knowledge_engine_mcp", "write", name) == [
        "knowledge:decision:execute"
    ]
    assert {
        "noesis-jev-epistemic-kind-suggestion-v1",
        "noesis-awareness-decision-suggestion-v1",
        "noesis-jev-awareness-evaluation-v1",
        "noesis-jev-answer-support-evaluation-v1",
    } <= set(server.knowledge_engine_capabilities.fn()["contracts"])
