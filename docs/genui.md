# Noesis Canvas — the fully generative, adaptive UI

The frontend **is** the canvas: there are no fixed views or routes. Every
screen is generated at runtime from a natural-language intent — *"compare
outlet framing on climate policy"*, *"who disagrees about AI regulation?"*
— as a validated **`ui-spec-v1`** document rendered from a panel registry,
so every generated panel keeps the terminal's live/demo fallback
behaviour. The single control is a **⌘K command bar** in the top bar — not
a chat composer: the client planner runs on every keystroke, showing its
parse of the intent (facets, topic, window, source type) and a live ghost
wireframe of the layout it will build before ⏎ commits it. An empty canvas
shows the live pipeline signal (dim entity constellation, movers with
deltas that generate coverage views, ingest stats) instead of a greeting;
panels assemble fitted to the request (spec spans are hints — the renderer
stretches each row to fill the grid). The sidebar
is only a canvas manager: open canvases persisted in localStorage —
there is no navigation to replace, and the example intents on the empty
canvas (news ones gated by the domain pack) are the sole shortcuts. The surface is styled with Tailwind + shadcn/ui components
(`apps/web/src/components/ui/`), themed to the terminal palette.

## Architecture

> A proposal to derive this catalog/planner layer from the repo's MCP
> servers is in [architecture/MCP_REARCHITECTURE_PLAN.md](architecture/MCP_REARCHITECTURE_PLAN.md).

```
intent ──► POST /api/v1/ui/generate ──► ui-spec-v1 ──► SpecRenderer
              │                                            │
              ├─ LLM planner (optional, key-gated)         ├─ panel registry
              ├─ heuristic planner (always available)      │  (20 renderers over
              └─ adaptivity:                               │   existing hooks/charts)
                  · warehouse data availability            └─ GenPanel chrome
                  · domain-pack ui_flags                       (pin / mute / badge)
                  · usage signals (pins, mutes, weights)
```

### Backend (`src/genui/`)

| Module | Role |
|---|---|
| `catalog.py` | Panel catalog: type → endpoint, warehouse tables, `ui_flag`, facets, layout defaults. Single source of truth — the frontend catalog and the contract enums are generated from it. |
| `codegen.py` | Stage 0 codegen: renders `apps/web/src/genui/catalog.gen.ts` and the `ui-spec-v1` contract enums from `catalog.py`. Run `python scripts/genui/codegen.py` after editing the catalog; CI (and `tests/unit/genui/test_codegen.py`) fail while the generated files are stale. |
| `spec.py` | `ui-spec-v1` dataclasses + pure-Python `validate_spec` (contract: `contracts/schemas/jsonschema/ui-spec-v1.json`). |
| `planner.py` | Heuristic planner: facet scoring from keyword evidence, topic / source-type / time-window extraction, panel assembly. No model, no network. |
| `adaptivity.py` | The adaptive inputs. Since R3 availability and `ui_flags` are tool-sourced (`resolve_availability` / `resolve_ui_flags` call the servers' stats tools through the MCP host, cached ~30s) with the DuckDB probe and pack registry as servers-down fallbacks; usage-signal re-ranking (`apply_signals`). Overview panels anchor on the `documents` corpus (union of `documents` and `news_articles`), so a zero-news corpus keeps a live overview. |
| `telemetry.py` | Pack-supplied empty-canvas telemetry (R3): enabled packs advertise `signals`/`movers`/`ticker` via `DomainPack.telemetry`; the engine's library fallback (recently ingested documents) fills whatever no pack supplies. |
| `llm.py` | Optional LLM planner (Anthropic or OpenAI). Any failure — no key, no SDK, bad JSON, invalid spec — falls back to the heuristic planner. |

Routes (`src/api/routes/genui_routes.py`, registered via the standard
feature-flag pattern in `src/api/app.py`):

- `POST /api/v1/ui/generate` — `{intent, source_type?, signals?}` → `{spec, meta}`
- `GET /api/v1/ui/context` — merged ui_flags, availability map (each with a
  `_source` field: `tools` when server-derived, `warehouse`/`packs` on
  fallback), LLM planner status, MCP host health
- `GET /api/v1/ui/telemetry` — pack-supplied ambient signal for the empty
  canvas (KPI signals, movers, ticker); with the news pack off it carries
  the library telemetry instead of an empty gap
- `GET /api/v1/ui/panels` — the panel catalog

### Discovery-derived catalog (`src/genui/discovery.py`, R2)

Tools annotated with a `meta.panel` block (format:
`docs/architecture/ADR-001-tool-panel-annotation.md`) become `PanelDef`s
at discovery time and merge over the static catalog, which stays the
fallback: `GET /api/v1/ui/panels` serves the merged catalog, and with no
servers connected its payload is byte-identical to the static one.
Annotated tools must declare an `outputSchema`; malformed annotations are
skipped with a warning, never an error. Every data-backed panel type has
an annotated counterpart across `tools/*_mcp` (`note` and `timeline` are
composed panels, exempt by design); the planner still reads the static
catalog until R3.

### MCP host runtime (`src/mcp_host/`, R1)

The API process supervises the repo's 12 `tools/*_mcp` stdio servers:
pooled sessions with lazy connect and capped-backoff restart, a TTL
discovery cache over `tools/list` (default 60s, `NOESIS_MCP_TTL`), and
health states (connected / degraded / down) surfaced in the `mcp` block of
`GET /api/v1/ui/context`. Status reads are lock-guarded snapshots — a hung
server can never stall a request. Server list comes from `.mcp.json`
(project python servers only). Kill switch: `NOESIS_MCP_HOST=off`;
`TESTING=true` short-circuits entirely; without the `mcp` client SDK the
host reports itself unavailable instead of failing. No planning behavior
reads from it yet — that lands with R2/R3 (discovery-derived catalog,
tool-sourced adaptivity).

### Frontend (`apps/web/src/genui/`)

- `spec.ts` — ui-spec-v1 wire types; re-exports the panel/facet unions and
  client catalog from `catalog.gen.ts` (generated from `src/genui/catalog.py`
  — never edit it by hand, run `python scripts/genui/codegen.py`).
- `Canvas.tsx` / `CommandBar.tsx` / `canvases.ts` — the app's only surface
  (ambient-signal empty state), the ⌘K command bar with live plan preview,
  and the canvas manager (open/activate/close, persisted per browser).
- `registry.tsx` — panel type → renderer (~20 types incl. library documents,
  watchlist and story timeline), reusing `lib/queries.ts` hooks and the SVG
  charts; unknown types render a stub, never crash.
- `useUiSpec.ts` — asks the backend planner, falls back to `planner.ts`
  (a slim TS mirror of the heuristic rules) when unreachable.
- `signals.ts` — localStorage usage signals: pin (always include + boost),
  mute (hide type), interaction weights. Fed back into every generation.
- `GenPanel.tsx` / `SpecRenderer.tsx` — panel chrome (pin/mute/provenance
  badge) and the 12-column grid.

The provenance strip above the canvas shows which planner ran:
`LLM PLAN` / `RULE PLAN` (backend) / `LOCAL PLAN` (client fallback).

## Adaptivity guarantees

- **Data-aware**: panels whose warehouse tables are empty are dropped and
  listed in the plan note ("Hidden for now…"). Availability is read from
  the MCP servers' stats tools when the host is up, from the DuckDB probe
  otherwise; unknown availability keeps every panel (frontend demo
  fallback covers empty endpoints).
- **Pack-aware**: panels gated by a domain-pack `ui_flag` disappear when
  the pack is disabled.
- **Usage-aware**: pins always include and boost a panel type; mutes hide
  it (restorable from the muted strip); interaction weights nudge ordering.
- **Never empty**: if adaptivity removes every data panel, the canvas falls
  back to the overview set.

## LLM planner configuration (optional)

| Env var | Meaning |
|---|---|
| `NOESIS_GENUI_LLM` | `auto` (default) or `off`. |
| `NOESIS_GENUI_PROVIDER` | `anthropic` or `openai`; auto-detected from which API key is set. |
| `NOESIS_GENUI_MODEL` | Model id override. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Provider credentials. |

Without a key the canvas is fully functional on the heuristic planner —
the spec shape and every downstream behaviour are identical.

## Tests

```
python3 -m pytest tests/unit/genui tests/unit/api/routes/test_genui_routes_smoke.py \
    tests/unit/api/routes/test_genui_routes_coverage.py -q
```

Contract fixtures live in `contracts/examples/ui-spec-v1/{valid,invalid}/`
and are validated by both the pure-Python validator and (when installed)
`jsonschema` against `contracts/schemas/jsonschema/ui-spec-v1.json`.
