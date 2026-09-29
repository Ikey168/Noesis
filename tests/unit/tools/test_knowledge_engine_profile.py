import asyncio

import pytest
from fastmcp import FastMCP

from tools.knowledge_engine_mcp.profiles import RESEARCH_TOOLS, apply_tool_profile


def test_research_profile_exposes_only_bounded_workflow_tools():
    mcp = FastMCP("profile-test")
    for name in sorted(RESEARCH_TOOLS | {"unrelated_tool"}):
        def fn() -> dict:
            return {}

        fn.__name__ = name
        mcp.tool(name=name)(fn)

    asyncio.run(apply_tool_profile(mcp, "research"))
    assert set(asyncio.run(mcp.get_tools())) == RESEARCH_TOOLS


def test_research_profile_rejects_missing_required_tools():
    mcp = FastMCP("profile-test")
    with pytest.raises(RuntimeError, match="missing tools"):
        asyncio.run(apply_tool_profile(mcp, "research"))
