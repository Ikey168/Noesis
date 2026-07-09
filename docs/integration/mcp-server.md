# Noesis as an MCP server (R13 / Stage 4)

The R0-R12 milestones made MCP servers *feed* the Noesis canvas. R13 turns the
pattern inside out: Noesis itself is exposed as an MCP server so an external
host (Claude Desktop, another agent) can ask Noesis to plan a view.

`tools/noesis_mcp/server.py` serves two tools:

- `noesis_generate_view(intent, source_type?, auth_token?)` returns a validated
  `ui-spec-v1` document, the same contract the canvas renders. It is a thin
  transport over the `/api/v1/ui/generate` semantics: it reuses the heuristic
  planner (`src.genui.plan`) and the domain-pack `ui_flags`, and validates the
  spec before returning, so an external host never receives an invalid document.
- `noesis_panels()` returns the panel catalog, so a host can see what a
  generated view may contain.

## Transport

Stdio by default (for local testing). Streamable HTTP when
`NOESIS_MCP_TRANSPORT=http`:

```bash
NOESIS_MCP_TRANSPORT=http NOESIS_MCP_HTTP_PORT=8100 python tools/noesis_mcp/server.py
```

An external host connects to `http://<host>:8100/mcp`. Host and port default to
`127.0.0.1:8100` (`NOESIS_MCP_HTTP_HOST` / `NOESIS_MCP_HTTP_PORT`). Both env vars
follow the R13 alias rule (`NEURONEWS_*` fallbacks resolve identically).

## Auth story

Minimal to start, and explicit:

- **Unset `NOESIS_MCP_AUTH_TOKEN` (default): open.** Intended for the localhost
  default bind, where the transport is not reachable off-box.
- **Set `NOESIS_MCP_AUTH_TOKEN=<token>`: required.** Every
  `noesis_generate_view` call must pass a matching `auth_token`; a missing or
  wrong token returns `{"error": "unauthorized: ..."}` and no spec.

For a network deployment, bind to a non-loopback host only behind a reverse
proxy that terminates TLS and enforces the bearer token (or FastMCP's own auth
providers), and set `NOESIS_MCP_AUTH_TOKEN` as a defense-in-depth check at the
tool layer. The token gate here is deliberately simple; it is the floor, not the
whole story.

## Note on server naming

This outward server is named `noesis` (not `neuronews-*`). The internal
`tools/*_mcp` servers that feed the canvas keep their `neuronews-*` names as
documented aliases (see `docs/development/naming.md`); they are not user-facing.
