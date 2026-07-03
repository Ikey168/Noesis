# ADR-002: Data-plane (Stage 3) go/no-go

Status: accepted. Part of R12 (Data-plane benchmark and Stage 3 decision) in
`MCP_REARCHITECTURE_PLAN.md`; records the Stage 3 gate with numbers.

## Context

Stage 1 made MCP the read/compose plane for the canvas: tools describe panels
(R2 discovery), and panels render from the REST routes and demo fallbacks.
Stage 3 asks a further question: should panel *data* also flow through MCP,
served to the browser by an API proxy (`POST /api/v1/ui/data`) that invokes a
data-mode tool, instead of the browser calling the REST route directly?

The case for it: one data path, one allowlist, one place to cache, and panels
that follow discovery all the way down to their rows. The case against it: the
MCP stdio hop and FastMCP serialization sit between the API and the warehouse,
and a fanned-out canvas is latency-sensitive.

R12 built the prototype to measure the tradeoff, not argue it:

- `articles_data` (pipeline server): a data-mode tool returning the full
  `articles` payload, field-for-field equivalent to `/api/v1/news/articles`.
- `POST /api/v1/ui/data`: an allowlist-gated, rate-limited, size-capped proxy,
  behind the `NOESIS_GENUI_DATA_PROXY` flag.
- `scripts/genui/dataplane_benchmark.py`: the benchmark transcribed below.

## Benchmark

200-row articles payload, 60 iterations, same warehouse, warm process. The
three paths run the identical query and return byte-identical payloads
(50,743 bytes each, which confirms the #619 equivalence contract). Latency:

| path | p50 (ms) | p95 (ms) | mean (ms) | payload (B) |
|---|---|---|---|---|
| `rest_direct` (in-process SQL the REST route runs) | 13.32 | 15.47 | 13.70 | 50,743 |
| `mcp_tool` (raw FastMCP client to `articles_data`) | 46.14 | 55.47 | 47.04 | 50,743 |
| `proxy_cold` (`/ui/data` handler, cache invalidated each call) | 50.38 | 64.75 | 51.82 | 50,743 |
| `proxy_cached` (`/ui/data` handler, shared TTL cache hit) | 0.36 | 0.44 | 0.37 | (cached) |

Reading the numbers:

- **Payloads are equivalent.** Same bytes on every path; data mode is a faithful
  full-payload variant, not a summary.
- **The cold MCP path costs ~3.8x REST**: +37 ms p50, +49 ms p95 per uncached
  call. That cost is the process boundary and FastMCP stdio serialization
  (`mcp_tool` alone is 46 ms), not the query. For a canvas fanning out many
  cold panels, that overhead compounds.
- **The cached path is ~37x faster than REST** (0.36 ms vs 13.32 ms), because
  the R4 shared `call_tool_cached` TTL cache serves repeat loads without
  reopening the warehouse. Repeat panel loads and multi-panel canvases that
  reuse a tool result land here.

## Decision

**Conditional GO.** Promote exactly one panel family, `articles`, to serve
through the proxy behind the `NOESIS_GENUI_DATA_PROXY` flag, cache-served. The
prototype ships enabled-off; a deployment that turns the flag on gets the
articles panel end-to-end through MCP, at parity on payload and at better than
REST latency once the TTL cache is warm.

We do **not** replace REST wholesale, and we do not move latency-sensitive
first loads onto the cold proxy path. The gating metric for a broader Stage 3
rollout is the cold-path overhead: bringing `proxy_cold` p95 within ~1.5x of
`rest_direct` (today it is ~4.2x). The overhead is transport, not
architecture, so the levers are known: a persistent warm session pool (R1
already supervises one in-process), a lighter result encoding than JSON-over-
stdio, and pre-warming the cache on generate. Until that metric is met, the
flag stays off by default and only the articles family is promoted.

### Retirement criteria for the prototype

If, by the next data-plane review, none of the cold-path levers has closed the
gap and no second panel family has a cache-friendly access pattern that
benefits, retire the proxy: delete `src/genui/dataplane.py`, the
`genui_data_routes` router, and the `articles_data` data block, and keep panels
on REST. The prototype is cheap to remove precisely because it is one flag, one
route, one tool.

## Consequences

- The browser still never speaks MCP; the proxy is the only door, and it is
  allowlist-gated (data-mode tools only), rate-limited per client, and
  size-capped both ways.
- Data-mode tools are a distinct annotation (`meta.data`) from panel tools
  (`meta.panel`), so planner-facing stats tools are untouched and a data tool
  can never be mistaken for a panel definition.
- The decision is reproducible: re-run `scripts/genui/dataplane_benchmark.py`
  to refresh the table.
