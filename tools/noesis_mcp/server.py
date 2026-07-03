"""
Noesis as an MCP server (MCP rearchitecture plan, R13 / Stage 4).

Turns the pattern inside out: the earlier milestones made MCP servers *feed*
the Noesis canvas; this one exposes **Noesis itself** as an MCP server so an
external host (Claude Desktop, another agent) can ask Noesis to plan a view.
`noesis_generate_view(intent)` returns a validated `ui-spec-v1` document, the
same contract the canvas renders, over Streamable HTTP.

It is a thin transport over the `/api/v1/ui/generate` semantics: it reuses the
heuristic planner (`src.genui.plan`) and the domain-pack `ui_flags`, and
validates the spec before returning it, so an external host never receives an
invalid document.

Transport: stdio by default (for local testing), or Streamable HTTP when
`NOESIS_MCP_TRANSPORT=http` (host `NOESIS_MCP_HTTP_HOST`, default 127.0.0.1;
port `NOESIS_MCP_HTTP_PORT`, default 8100). Auth: when
`NOESIS_MCP_AUTH_TOKEN` is set, every `noesis_generate_view` call must pass a
matching `auth_token`; unset means open (local-only default). See
`docs/noesis-mcp-server.md` for the full auth story.

Design constraints (as for every tool server): stdlib + fastmcp at import
time, lazy imports inside tools.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config.env import resolve_env  # noqa: E402

mcp = FastMCP("noesis")


def _auth_ok(auth_token: Optional[str]) -> bool:
    """A token is required only when NOESIS_MCP_AUTH_TOKEN is configured."""
    required = resolve_env("MCP_AUTH_TOKEN")
    if not required:
        return True
    return auth_token == required


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "spec": {"type": "object"},
            "meta": {"type": "object"},
        },
        "additionalProperties": True,
    },
)
def noesis_generate_view(
    intent: str,
    source_type: Optional[str] = None,
    auth_token: Optional[str] = None,
) -> dict:
    """Plan a Noesis canvas view for a natural-language intent and return a
    validated ``ui-spec-v1`` document (the same contract the canvas renders).

    Args:
        intent: the analyst intent, e.g. "who disagrees about AI regulation?".
        source_type: optional source-type filter (news, blog, paper, ...).
        auth_token: required only when NOESIS_MCP_AUTH_TOKEN is set.
    """
    if not _auth_ok(auth_token):
        return {"error": "unauthorized: a valid auth_token is required"}
    try:
        from src.genui.adaptivity import merged_ui_flags
        from src.genui.planner import plan
        from src.genui.spec import MAX_INTENT_LENGTH, SOURCE_TYPES, validate_spec
    except Exception as exc:  # pragma: no cover - defensive import guard
        return {"error": f"planner unavailable: {exc}"}

    if source_type is not None and source_type not in SOURCE_TYPES:
        return {"error": f"source_type must be one of {sorted(SOURCE_TYPES)}"}
    intent = (intent or "")[:MAX_INTENT_LENGTH]

    try:
        ui_flags = merged_ui_flags()
    except Exception:
        ui_flags = {}
    try:
        spec = plan(intent, source_type=source_type, ui_flags=ui_flags)
        spec_dict = spec.to_dict()
        errors = validate_spec(spec_dict)
        if errors:
            return {"error": f"generated spec failed validation: {'; '.join(errors[:3])}"}
        return {
            "spec": spec_dict,
            "meta": {"generated_by": spec.generated_by, "ui_flags": ui_flags},
        }
    except Exception as exc:
        return {"error": f"view generation failed: {exc}"}


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"panels": {"type": "array"}, "count": {"type": "integer"}},
        "additionalProperties": True,
    },
)
def noesis_panels() -> dict:
    """The Noesis panel catalog, so an external host can see what a generated
    view may contain."""
    try:
        from src.genui.catalog import panel_catalog_dict

        panels = panel_catalog_dict()
        return {"panels": panels, "count": len(panels)}
    except Exception as exc:
        return {"error": str(exc)}


def main() -> None:
    transport = (resolve_env("MCP_TRANSPORT", "stdio") or "stdio").lower()
    if transport in ("http", "streamable-http", "streamable_http"):
        host = resolve_env("MCP_HTTP_HOST", "127.0.0.1")
        port = int(resolve_env("MCP_HTTP_PORT", "8100"))
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
