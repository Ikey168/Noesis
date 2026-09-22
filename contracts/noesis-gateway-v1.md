# noesis-gateway-v1 — default MCP surface

The `noesis` MCP server is the default agent-facing entry point for Noesis.
It intentionally exposes a small daily-driver surface instead of mirroring every
specialist MCP server.

## Endpoint

`noesis serve` starts the gateway over Streamable HTTP by default:

```text
http://127.0.0.1:8100/mcp
```

Use `--transport stdio` for a spawned local process. The raw
`tools/noesis_mcp/server.py` entry point also follows the shared MCP transport
environment variables.

## Tools

- `domains` — configured knowledge domains;
- `add` — ingest a file or HTTP(S) URL through the production
  ingest → extract → resolve → index workflow;
- `search` — cited document search;
- `ask` — deterministic cited answers;
- `brief` — bounded change brief;
- `documents`, `claims` — direct evidence inspection;
- `inspect_source` — source metadata plus integrity evidence;
- `coverage` — backing/source/freshness coverage;
- `watch` — create/list/poll/pause/resume/delete durable watches;
- `export` — return answer, claim, or integrity Evidence Bundles inline.

Query tools preserve the existing `noesis-kb-v1` results rather than inventing
a second knowledge contract. Gateway-specific results and errors use
`noesis-gateway-v1`.

## Default domain

When no domain is supplied, the gateway selects `local` when configured. If
there is no `local` domain and exactly one domain exists, it selects that
domain. Otherwise the operation returns `domain_required`; the gateway never
silently searches an arbitrary domain.

## Mutability and privacy

`add` is a write. `watch` is conservatively classified as a write because
some actions mutate durable state. Evidence exports from a private domain
require `include_private=true`; there is no silent private-evidence export.

The specialist MCP servers remain supported for advanced capabilities and
fine-grained authorization. Current production code should treat the gateway as
the default connection target, not as a replacement implementation of those
subsystems.
