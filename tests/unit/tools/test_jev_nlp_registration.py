from __future__ import annotations

import asyncio

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_source_bound_jev_nlp_suggestions_are_public_and_scoped():
    names = {
        "suggest_jev_claim_presence",
        "suggest_jev_checkworthiness",
        "suggest_jev_sentiment",
        "suggest_jev_attribution",
        "suggest_jev_stance",
        "suggest_jev_frames",
    }
    tools = asyncio.run(server.mcp.get_tools())
    assert names <= tools.keys()
    for name in names:
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:decision:execute"
        ]
    assert {
        "noesis-jev-claim-presence-suggestion-v1",
        "noesis-jev-checkworthiness-suggestion-v1",
        "noesis-jev-sentiment-suggestion-v1",
        "noesis-jev-attribution-suggestion-v1",
        "noesis-jev-stance-suggestion-v1",
        "noesis-jev-frame-suggestion-v1",
        "noesis-jev-mining-evaluation-v1",
    } <= set(server.knowledge_engine_capabilities.fn()["contracts"])
