from __future__ import annotations

import asyncio

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_jev_evidence_suggestions_are_public_and_scoped():
    names = {
        "suggest_jev_shortlist_rerank",
        "suggest_jev_answer_support",
        "suggest_jev_claim_relation",
    }
    tools = asyncio.run(server.mcp.get_tools())
    assert names <= tools.keys()
    for name in names:
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:decision:execute"
        ]
    assert {
        "noesis-jev-rerank-suggestion-v1",
        "noesis-jev-answer-support-suggestion-v1",
        "noesis-jev-claim-relation-suggestion-v1",
        "noesis-jev-rerank-evaluation-v1",
        "noesis-jev-claim-relation-evaluation-v1",
    } <= set(server.knowledge_engine_capabilities.fn()["contracts"])
