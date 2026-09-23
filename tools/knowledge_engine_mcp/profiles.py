"""Bound the MCP tool list for clients that cannot expose the full surface."""

from __future__ import annotations


RESEARCH_TOOLS = frozenset({
    "discover_intake_modes",
    "preflight_intake_mode",
    "start_intake_mode",
    "start_intake_research_topic",
    "inspect_intake_mode",
    "command_intake_mode",
    "export_intake_mode",
    "verify_intake_mode_export",
    "document_revision",
    "inspect_intake_research_progress",
    "assess_intake_research_progress",
    "inspect_intake_research_assessment",
    "save_intake_research_bundle",
    "inspect_intake_research_bundle",
    "export_intake_research_bundle",
    "verify_intake_research_bundle_export",
})


async def apply_tool_profile(mcp, profile: str) -> None:
    """Expose a small research workflow without changing the default server."""
    if profile == "all":
        return
    if profile != "research":
        raise ValueError(f"unknown Noesis MCP tool profile: {profile}")
    registered = set(await mcp.get_tools())
    missing = RESEARCH_TOOLS - registered
    if missing:
        raise RuntimeError(f"research MCP profile is missing tools: {sorted(missing)}")
    for name in registered - RESEARCH_TOOLS:
        mcp.remove_tool(name)
