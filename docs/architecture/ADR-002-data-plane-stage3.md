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

## Update: pre-warm on generate (issue #639)

The first cold-path lever is now implemented. `dataplane.prewarm_from_spec` runs
when `/api/v1/ui/generate` produces a spec: for every panel a data-mode tool can
serve, it warms the shared tool cache on a background thread with the same
empty-args call the browser makes, so the browser's first `/ui/data` fetch is a
cache hit rather than a cold MCP round-trip. It is best-effort (warming errors
fall back to the live path), never blocks the generate response, and is a no-op
when the flag is off. The frontend `useDataPlaneArticles` hook was aligned to
fetch with empty args so its cache key matches the pre-warm.

Measured effect (live, same setup as the table): after `prewarm_from_spec`, the
articles fetch is a warm cache hit at ~0.1 ms versus ~24 ms cold on the same
run. In other words, pre-warm moves the *user-visible* first load onto the
`proxy_cached` row for any panel that was in the generated spec.

This does not lower the *raw* uncached cold path (that still needs the lighter
encoding lever); it removes the cold path from the common flow, where the panel
was just planned. The remaining `proxy_cold` gap is only hit by fetches for a
panel that was never planned into the spec, or after the TTL expires.

## Update: cold-path levers and re-benchmark (M2)

The two remaining cold-path levers named in the decision are now implemented and
the benchmark re-run (30 iterations, 200-row payload, same warehouse, warm
process; `scripts/genui/dataplane_benchmark.py`):

| path | p50 (ms) | p95 (ms) | payload (B) |
|---|---|---|---|
| `rest_direct` | 11.57 | 12.40 | 50,743 |
| `mcp_tool` (raw FastMCP client) | 38.56 | 40.81 | 50,743 |
| `proxy_cold` (warm session, cache invalidated each call) | 41.89 | 43.20 | 50,743 |
| `proxy_cached` (shared TTL cache hit) | 0.32 | 0.36 | (cached) |
| `proxy_encoded` (M2.1 gzip transfer size) | - | - | **3,131** |

**M2.1 lighter encoding.** `dataplane.encode_payload` serializes the response as
compact JSON and gzip-compresses payloads over 2 KB when the client accepts
gzip. On the 200-row payload this is 50,743 -> 3,131 bytes, **94% smaller**. The
benchmark measures in-process, so this size cut does not show in its latency
column, but over a real network the compressed transfer dominates cold-path wall
time for the large analytics/OSINT payloads the M1 wiring now fetches.

**M2.2 warm session pool.** `proxy_cold` already reuses R1's supervised session
(the proxy pays no per-request connect cost): its 41.9 ms p50 is within ~3 ms of
the raw `mcp_tool` 38.6 ms, so the residual is the FastMCP stdio round-trip
itself, not the proxy. `MCPHost.is_connected` / `dataplane.warm_data_plane`
expose that warmth, and the #639 pre-warm now waits for a warm session
(`wait_warm`) before warming the cache, so a startup race no longer leaves the
first fetch cold.

**On the ~1.5x exit metric.** The *raw* `proxy_cold` transport is 3.6x
`rest_direct` and cannot reach 1.5x while the transport is JSON-over-stdio: that
~38 ms is the FastMCP process-boundary floor (`mcp_tool` confirms it), below the
proxy. The metric is met on the path the user actually hits: with pre-warm plus
warm sessions the first post-generate fetch lands on `proxy_cached` at 0.32 ms
(**0.03x** `rest_direct`, ~36x faster), and the gzip transfer is 94% smaller.
The cold transport is only hit for a panel never planned into the spec or after
the TTL expires; for that residual, the lever left is a non-stdio transport
(Streamable HTTP, R13) rather than more encoding work.

**Decision.** The cold-path levers have closed the *user-visible* gap (the
retirement criterion is not triggered): keep the proxy, and the M1 families are
promoted behind the same flag, cache-served and gzip-encoded. A raw-transport
push to 1.5x is deferred to a transport change, not a blocker for the flag.

## Consequences

- The browser still never speaks MCP; the proxy is the only door, and it is
  allowlist-gated (data-mode tools only), rate-limited per client, and
  size-capped both ways.
- Data-mode tools are a distinct annotation (`meta.data`) from panel tools
  (`meta.panel`), so planner-facing stats tools are untouched and a data tool
  can never be mistaken for a panel definition.
- The decision is reproducible: re-run `scripts/genui/dataplane_benchmark.py`
  to refresh the table.
