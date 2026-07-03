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
| `llm.py` | Optional LLM planner (Anthropic or OpenAI). Since R4, with the MCP host up it runs as a bounded tool-use loop: the model may call a read-only inspection allowlist (stats/listing tools) to ground the layout in what data exists, then emits the spec. The loop is budgeted (`NOESIS_GENUI_LOOP_BUDGET_MS`, default 9000; `MAX_TOOL_ROUNDS` = 3) and degrades to one-shot planning when the budget would be blown. Any failure — no key, no SDK, bad JSON, invalid spec — falls back to the heuristic planner. |

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

### Analytics plane (`src/analytics/`, R5)

Data-science techniques exposed as canvas capabilities under a
**statistical-honesty contract**: every analytic output carries its sample
size, method and assumptions, and no headline figure ships without an
interval (`honesty.py`; `validate_analytic_output` is the gate the contract
tests use). Fits run as batch jobs (`framework.py`: `AnalyticJob` +
`run_job` → result table → optional MLflow) and the MCP tools *read* the
result tables. Wave 1a ships `detect_anomalies` (robust z-score over
per-topic coverage/sentiment, the `anomaly_timeline` panel),
`score_confidence` (bootstrap CI, error bars on `outlet_ranking`) and
`stance_significance` (permutation test). Wave 1b (R6) adds `lead_lag`
(cross-correlation who-leads matrix), `cluster_narratives` (lexical
narrative threads), `kg_communities`/`kg_centrality` (community-coloured,
centrality-sized entity graph) and `semantic_drift` + `forecast_topic`
(the `drift_trajectory` and `forecast` panels; forecasts are always
banded). Maths is pure stdlib (`stats.py`, `graph.py`, `text.py`), so the
tool servers stay import-safe. The panels render the uncertainty fields
today from demo data; live panel data arrives with the MCP data proxy
(R12).

### Research pack (`src/domains/research/`, R7)

The second first-class domain, proving pack-plurality. Paper-metadata
enrichers (venue, citations, concept) plus `ui_flags` gating a research
panel family: `venues` (credibility generalizing the outlet transparency
score to publication venues), `citation_graph` (paper citation network),
`literature_claims` (claims scoped to papers). Served by
`tools/research_mcp/` and surfaced through R2 discovery; research-flavored
telemetry (recent papers, emerging concepts) takes over the empty canvas
when the pack dominates. Enable with `NEURONEWS_ENABLED_PACKS=research`;
with news off the canvas is fully functional on research panels.

### Provisioning plane (`src/provisioning/`, R8)

Turns MCP from the read/compose plane into one that can *provision* knowledge
domains: an agent deploys a namespaced knowledge graph, binds the sources that
feed it (explicitly or by a quality criterion), routes matching documents into
the namespace, and the canvas grows a `provisioned_kg` panel for it via R2
discovery. A per-KG namespace is a table prefix (`kg_<name>_documents` /
`_entities` / `_claims`); routing copies only the rows whose `source` is bound
to the KG out of the shared corpus, so a namespace holds only routed documents
and the shared tables are read, never mutated. `namespaces.py` owns the prefix
DDL and routing (the KG name is validated to `[a-z][a-z0-9_]*` before it ever
reaches a table identifier); `store.py` is the registry plus an append-only
lineage event log, every write an idempotent upsert keyed by name; `guardrails.py`
enforces the RW guardrails (max KGs, max sources per KG, an ingest rate cap,
a deploy approval gate, a teardown confirm gate); `provisioner.py` ties them
into `deploy` / `attach_sources` / `ingest` / `status` / `list` / `teardown`,
resolving criteria (transparency, attribution, type) against `outlet_scores`.
Served by `tools/provisioning_mcp/`: the write tools (`kg_deploy`,
`kg_attach_sources`, `kg_ingest`, `kg_teardown`) hold a process lock over a
read-write warehouse, mirroring the pipeline server's trigger tools; the read
tools (`kg_status`, `kg_list`, `kg_lineage`, `kg_view`) open it read-only.
Deploy and teardown are approval-gated by default (a free dry-run preview
otherwise); teardown archives (renames the namespace tables aside, never
deletes) and detaches sources; re-running a failed provision converges without
duplicating. Every deploy/attach/ingest/teardown is in the lineage log, so each
step is visible.

R9 is the acceptance of that plane: two domains, `finance` (earnings-call
transcripts) and `legal` (policy filings), stand up over the real
`provisioning_mcp` server with no pack code, each deploy-to-ingested in roughly
250 ms, each namespace scoped to its own routed documents. `kg_view(kg)` returns
the scoped documents/entities/claims family the `provisioned_kg` panel renders.
The harness is `scripts/provisioning/acceptance.py`, the regression is
`tests/unit/provisioning/test_acceptance.py`, and the write-up (with the
friction fed back into R8) is `docs/provisioning-acceptance.md`.

### OSINT composition (`src/osint/`, R10)

Defensive, analytical primitives over already-ingested public documents, each a
pure composition of layers Noesis already builds (M6 claims, the RAG evidence
links, the semantic conflict edges, the outlet/source scores). Nothing crawls,
targets or de-anonymizes; the tools only read the warehouse.
`corroborate(claim_id)` counts the independent sources (by distinct outlet,
excluding the claim's own source) that support or contradict a claim, each
weighted by its transparency composite, and flags a claim as `single_sourced`
rather than emitting a single confidence number. `source_reliability(source)`
generalizes the outlet transparency score to any source_type (blogs, papers,
filings) and adds a corroboration hit-rate and a disputed-claim rate,
honesty-wrapped with a track-record-weighted interval. `contradiction_scan(topic
| entity)` reads the CONTRADICTS edges and joins each pair back to both sources
and citations, flagging uncited entries rather than hiding them. Served by
`tools/osint_mcp/` (read-only) and surfaced through R2 discovery under the
`osint` `ui_flag` as the `corroboration`, `reliability_card` and
`contradiction_ledger` panels. The panels render the uncertainty and
citation fields from demo data today; live panel data arrives with the MCP
data proxy (R12).

The investigation surface (R11) extends the same package under one evidence
discipline (`src/osint/evidence.py`: a citation on every line, the cited /
single_sourced / uncited render states). `entity_dossier(entity)` is a cited
brief (mentions, aliases, first/last seen, connected entities) with a
person-entity guardrail enforced in code: a person with no ingested document
is refused, never described from inference. `relationship_path(a, b)` is the
shortest co-mention path between two entities with the establishing documents
cited on every edge and resolution ambiguity surfaced.
`timeline_reconstruct(topic | entity)` buckets dated cited claims into events,
each carrying its corroboration density. An investigation is a Track
P-provisioned KG reconstructable from its audit trail (`investigation_audit`),
and the OSINT-dominant empty canvas leads with open threads / newly
corroborated / newly contradicted. The most abusable tools
(`geolocate_claims`, `narrative_coordination`) stay behind the review gate in
`docs/osint-review-gate.md`, absent from the served surface until it passes.
These surface as the `entity_dossier`, `relationship_path` and
`evidence_timeline` panels.

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
