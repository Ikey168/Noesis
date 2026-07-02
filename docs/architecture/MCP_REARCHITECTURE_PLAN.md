# Rearchitecting Noesis around MCP

**Status:** Proposal (design review requested)
**Date:** 2026-07-02
**Scope:** capability layer, generative-UI planner, panel data plane
**Related:** `docs/genui.md`, `docs/architecture/KNOWLEDGE_ENGINE_PIVOT_PLAN.md`

## Summary

Noesis maintains two parallel capability surfaces: ~30 FastAPI REST routes
consumed by the generative canvas, and 12 FastMCP stdio servers
(`tools/*_mcp/`) consumed by development agents. Every subsystem —
argument mining, pipeline, knowledge graph, feeds, domain packs, lineage,
contracts, monitoring — is wrapped twice, and the generative-UI panel
catalog is hand-mirrored in three more places (`src/genui/catalog.py`,
`apps/web/src/genui/spec.ts`, `contracts/schemas/jsonschema/ui-spec-v1.json`).

This proposal makes MCP the **capability and control plane**: panels,
planner inputs, and domain-pack state derive from MCP tool discovery
instead of hand-maintained registries. The REST layer remains the
high-volume **data plane** initially; the browser never speaks MCP
directly. Migration is staged so every stage ships value independently
and stage N never blocks on stage N+1. Track P extends the plan from
read/compose to **provisioning**: MCP tools that deploy new knowledge
graphs and select the sources that feed them, with the canvas growing
panels for new domains via discovery alone. Track DS applies the same
pattern to **analytics**: data-science techniques (anomaly detection,
lead-lag analysis, narrative clustering, graph science, significance
testing) exposed as annotated tools the planner can compose into layouts.

## Current state

| Concern | Today | Problem |
|---|---|---|
| Capabilities | REST routes in `src/api/routes/*` **and** MCP tools in `tools/*_mcp/` | Every feature implemented and documented twice; surfaces drift |
| Panel catalog | Hand-written in `catalog.py`, mirrored in `spec.ts` + contract enum | Triple mirror; the genui review found real drift defects and the sync is test-enforced by hand |
| Domain packs | Registry + `config/domain_packs.json` + `ui_flags` dict | A pack is *conceptually* a capability bundle — exactly what an MCP server is |
| Data availability | Bespoke DuckDB probe in `src/genui/adaptivity.py` | Duplicate of what `am_stats` / `get_stats`-style MCP tools already report |
| LLM planner | One-shot JSON completion over a static catalog dump | Plans blind; cannot inspect data before composing a layout |
| Dev agents | `.mcp.json` wires 12 servers (read-only, token-thin by design) | Right for debugging; payloads too thin for UI data |

## Target architecture

```
                       ┌────────────────────────────────────────────┐
 browser (apps/web)    │ FastAPI backend = MCP HOST                 │   MCP servers (FastMCP)
 ───────────────────►  │                                            │  ┌──────────────────────┐
  REST (unchanged):    │  genui planner ──── MCP client sessions ───┼─►│ neuronews-arguments  │
  /api/v1/ui/generate  │   · catalog ⇐ tool discovery               │  │ neuronews-pipeline   │
  /api/v1/ui/panels    │   · availability ⇐ stats tools             │  │ neuronews-kg         │
  /api/v1/ui/data*     │   · LLM planning ⇐ bounded tool-use loop   │  │ neuronews-blog-feeds │
  (*stage 3)           │   · packs ⇐ server presence                │  │ … (12 servers)       │
                       └────────────────────────────────────────────┘  └──────────────────────┘
```

Component mapping:

| genui concept | MCP concept |
|---|---|
| Panel type | Tool annotated with a renderer hint (`panel:*` tag + `outputSchema`) |
| Panel catalog | Aggregated `tools/list` across connected servers |
| Domain pack enabled | Server connected (pack config ⇒ which servers to spawn) |
| `ui_flags` | Server presence + per-tool availability |
| Data availability | Stats tools (`am_stats`, `get_stats`, …) instead of raw DuckDB probing |
| LLM planner context | Live tool discovery + tool calls, not a static catalog dump |

## Decisions

1. **The browser never speaks MCP.** The backend is the single MCP host;
   the frontend keeps its typed REST client, demo fallback, and the
   `ui-spec-v1` contract unchanged. (Rationale: transport simplicity, the
   existing WAF/JWT/RBAC middleware stays authoritative, and the offline
   client planner keeps working.)
2. **Transport:** in-process/stdio FastMCP sessions supervised by the
   backend for the 12 local servers; Streamable HTTP only if/when a server
   moves off-box. No per-request process spawning — sessions are pooled
   and health-checked.
3. **`ui-spec-v1` stays the wire format.** MCP changes where the catalog
   and data come from, not what the frontend renders. Tools that feed
   panels must declare `outputSchema`; the generic renderers key off it.
4. **REST remains the hot data path until Stage 3 proves otherwise.**
   Panel fetches are high-fanout (a canvas issues 5–10 parallel queries);
   we do not put a tool-call hop in that path until the proxy shows
   acceptable latency.
5. **DuckDB single-writer discipline is unchanged.** MCP servers stay
   read-only against the warehouse (as today); RW "trigger" tools keep
   going through the API process which owns the write lock.

## Staged migration

### Stage 0 — kill the mirror by codegen (no MCP required)
Generate `spec.ts`'s `PanelType`/`PANEL_CATALOG` and the contract enums
from `catalog.py` (extend `scripts/contracts/codegen.py` or a small
`scripts/genui/codegen.py`; CI check that generated files are current).
*Exit:* one source of truth; drift becomes a build error.
*Effort: small. Risk: minimal.*

### Stage 1 — MCP-derived catalog
Backend catalog builder opens MCP sessions at startup, lists tools, and
maps annotated tools (`panel:articles`, `panel:claims`, …) into
`PanelDef`s; unannotated tools are ignored. Static catalog remains as the
fallback when servers are down. `GET /api/v1/ui/context` reports
per-server health; `merged_ui_flags`/availability read from server
presence + stats tools with the DuckDB probe as fallback.
*Exit:* dropping a new annotated MCP server surfaces a new panel type in
`/api/v1/ui/panels` with zero code changes to genui.
*Effort: medium. Risk: server lifecycle management (supervision, startup
latency) — mitigated by lazy connect + cached discovery.*

### Stage 2 — grounded LLM planning
`plan_with_llm` becomes a bounded agentic loop (the backend as MCP host):
the model may call read-only inspection tools (≤ N calls, per-call and
total timeouts, allowlist) before emitting the final `ui-spec-v1` JSON,
which still passes `_sanitize` + `validate_spec` + usage-signal
enforcement. Heuristic planner untouched and still the no-key default.
*Exit:* with a key configured, plans demonstrably reflect actual data
(e.g. skips stance panels when `am_stats` shows zero rows) — testable by
mocking the MCP client.
*Effort: medium. Risk: latency and cost per generate — mitigated by the
call budget, caching stats results, and keeping the loop optional.*

### Stage 3 — MCP-backed panel data (evaluate before committing)
Add full-payload variants to the thin dev tools ("data mode"), plus
`POST /api/v1/ui/data {tool, args}` — an allowlisted, rate-limited proxy
the frontend uses for panels whose `PanelDef` names a tool instead of a
REST endpoint. Generic renderers (table / stat / list keyed off
`outputSchema`) display any new tool's data until a bespoke renderer is
registered.
*Exit:* a brand-new capability (server + annotation) renders end-to-end
with no frontend deploy.
*Effort: large. Risk: latency on the hot path, payload discipline, proxy
security surface — gate on a benchmark against the equivalent REST route.*

### Stage 4 (optional) — Noesis as an MCP server
Expose `noesis_generate_view(intent) -> ui-spec-v1` (+ claim/stance query
tools) over Streamable HTTP so external MCP hosts (Claude Desktop, other
agents) can drive Noesis; the spec document is the resource. Pairs with
the emerging MCP-apps/embedded-UI pattern when hosts support it.
*Effort: small once Stages 1–2 exist.*

## Track P — the provisioning plane: agent-deployed knowledge graphs

Stages 0–4 make MCP the *read/compose* plane. The further step is letting
MCP **provision knowledge domains**: an agent (or an operator through one)
deploys a new knowledge graph, selects the sources that feed it, and the
generative canvas grows panels for it — with no code change and no deploy.

Today the pieces exist but do not compose: `graph_builder` /
`enhanced_graph_populator` build the KG, `kg_updater` updates it per
ingested document, `blog_mcp.subscribe_feed` adds feeds,
`pipeline_mcp.run_connector` runs ingestion, `sources_mcp` profiles
outlets — and `kg_mcp` is read-only. Track P wires them into a domain
factory behind a small RW tool surface (a new `provisioning_mcp` server,
or RW tools added to `kg_mcp`):

```
kg_deploy(name, description, ontology?)        -> namespaced KG (registered in lineage)
kg_attach_sources(kg, sources[] | criteria)    -> bind feeds/connectors; criteria can
                                                  select via sources_mcp profiles
                                                  (e.g. transparency >= 0.7, type=paper)
kg_ingest(kg, backfill_days?)                  -> run bound connectors -> enrichers ->
                                                  graph population, async with progress
kg_status(kg) / kg_list()                      -> entity counts, source health, lag
kg_teardown(kg, confirm)                       -> archive + detach (never silent delete)
```

Dataflow: `kg_deploy` creates a **namespace** (per-KG table prefix or
graph partition — decided below), registers the namespace in lineage
(`lineage_mcp` already models namespaces), and materializes a runtime
domain pack whose MCP server annotation makes the canvas's Stage-1
discovery surface a scoped `entity_graph` / `documents` / `claims` panel
family for it automatically. `kg_attach_sources` resolves either an
explicit source list or a *criteria query* answered by `sources_mcp`
(this is the interesting part: source selection can be quality-driven —
"feed this KG only from outlets with attribution_rate ≥ X"). Ingestion
reuses the existing connector → contract → enricher → `kg_updater` path;
provisioning adds routing (which KG namespaces a document lands in),
not a new pipeline.

Guardrails (non-negotiable for RW provisioning):

| Concern | Rule |
|---|---|
| Write authority | Provisioning tools execute in/through the API process that owns the DuckDB write lock; MCP servers themselves stay read-only against the warehouse |
| Blast radius | Quotas: max KGs, max sources per KG, ingest rate caps; `kg_teardown` archives, requires `confirm`, never cascades to shared tables |
| Human-in-the-loop | Deploy/teardown are approval-gated by default (host-side confirmation); `kg_status` and dry-run previews are free |
| Provenance | Every deploy/attach/ingest registers lineage events; a KG's canvas panels can show "fed by N sources, selected because …" |
| Idempotency | `kg_deploy` and `kg_attach_sources` are upserts keyed by name; re-running a failed provision converges instead of duplicating |

Sequencing: Track P depends on Stage 1 (discovery is what makes a new KG
appear in the UI) and benefits from Stage 2 (a grounded planner can
propose provisioning: "no data on semiconductors — deploy a KG fed by
these four sources?" — surfaced as a suggestion, executed only on
approval). It does not depend on Stage 3.
*Effort: large (namespacing + routing are the real work). Risk: RW agent
surface — mitigated by the guardrail table above.*

## Track DS — the analytics plane: data-science techniques as MCP tools

The same discovery pattern that turns servers into panels turns
**analytical techniques into canvas capabilities**: an `analytics_mcp`
server (or tools added to `kg_mcp` / `argument_mcp` / `monitoring_mcp`)
whose annotated tools the planner can select for matching intents —
"anything unusual in climate coverage this month?" plans an anomaly
panel the same way "sentiment for energy" plans a heatmap today. The
stack already carries scikit-learn, embeddings + Qdrant/pgvector,
transformers, MLflow and dbt; Track DS is about *exposing* techniques,
mostly not inventing them.

Candidate tools, ordered by value-for-mission (source transparency and
argument analysis), not by novelty:

| Tool | Technique | Canvas panel | Why it earns its keep |
|---|---|---|---|
| `detect_anomalies(topic?, metric)` | Changepoint / robust z-score / seasonal-ESD on coverage volume & sentiment series | Anomaly timeline with flagged windows | Cheap, statistically honest, and the single most useful "what should I look at?" signal for a news terminal |
| `lead_lag(topic, outlets?)` | Cross-correlation / Granger-style lead-lag on per-outlet coverage series | "Who leads, who follows" matrix | Unique to the transparency mission: distinguishes agenda-setters from followers |
| `score_confidence(outlet)` / `stance_significance(a, b, topic)` | Bootstrap CIs on outlet scores; permutation / χ² tests on stance splits | Ranking panels grow error bars & significance badges | Makes the published transparency rankings *defensible* instead of point estimates |
| `cluster_narratives(topic?, window?)` | HDBSCAN over document embeddings (already computed for RAG/vector search) | Narrative-thread panel: competing storylines per topic | Upgrades event clustering from "same event" to "same narrative"; also powers claim dedup |
| `kg_communities(kg?)` / `kg_centrality(kg?)` / `kg_link_predict(kg?)` | Louvain/Leiden, PageRank/betweenness, link prediction | Community-colored entity graph; "likely next connections" | The KG exists; graph science is the cheapest deepening — and Track P KGs inherit it via the `kg` namespace param |
| `semantic_drift(term, window)` | Embedding drift of a term/entity over time windows | Drift trajectory panel | Complements stance drift: *meaning* shift, not just position shift |
| `forecast_topic(topic, horizon)` | Exponential smoothing / lightweight ARIMA on velocity | Forecast band on trend panels | Flashy but noisiest for news; ship last, always with intervals |
| `model_drift_report()` | PSI/KS data-drift + score-drift vs benchmark baselines | Model-health panel | Ties the existing `monitoring_mcp` + benchmark gate into the canvas |

### Wave 2 — deeper techniques (after Wave 1 proves the pattern)

| Tool | Technique | Canvas panel | Why it earns its keep |
|---|---|---|---|
| `coverage_bias(source)` | Agenda-setting analysis: log-odds with Dirichlet prior ("Fightin' Words", Monroe et al.) of an outlet's topic mix vs the corpus baseline | "What X over/under-covers" diverging bars | The sharpest transparency tool on this list: *selection* bias, which framing metrics miss entirely — and it is one SQL aggregation + a closed-form statistic |
| `coordination_detect(window?)` | Lockstep detection: correlation clustering over outlets' publish-timing/framing/narrative vectors; flags improbably synchronized groups | Coordination graph with cohort highlights | Coordinated-narrative / astroturf detection — mission-critical for a disinformation-aware platform, and a genuine differentiator |
| `burst_detect(term?, topic?)` | Kleinberg burst model on term/entity streams | Burst timeline (onset, intensity, decay) | The canonical news-stream algorithm; sharper than generic anomaly detection for term-level events; near-zero dependencies |
| `event_impact(topic, event_date)` | Event-study / difference-in-differences: coverage & sentiment vs a synthetic control basket of topics | Before/after impact panel with effect size + CI | Turns "did the announcement change the coverage?" from vibes into an estimate; scipy-only |
| `story_cascade(topic)` | Hawkes (self-exciting) process fit on cross-outlet publication times | Cascade tree / intensity curve | The natural mechanistic model of how stories propagate; upgrades `lead_lag` from correlation to dynamics |
| `story_survival(kind?)` | Kaplan–Meier survival curves + hazard factors for narrative lifetimes | "How long do stories like this live?" curve | Cheap (closed-form), visual, and feeds forecasting honesty: expected remaining lifetime with bands |
| `outlet_stylometry(source)` / `rhetoric_profile(source)` | Readability, hedging/certainty markers, emotional-language ratios, loaded-language lexicons | Style fingerprint radar per outlet | Enriches transparency profiles beyond *what* outlets say into *how*; lexicon fallback is pure house style |
| `claim_certainty(claim_id?)` | Epistemic-marker scoring (hedged vs asserted) on claims | Certainty column in claim panels | Complements fact-check verdicts: flags confident assertions of unverified claims — the risky quadrant |
| `novelty_score(document_id)` | KL divergence of a document's content vs the trailing corpus | "Actually new here" ranking in feeds | Anti-redundancy signal readers feel immediately; also a dedup assist |
| `model_calibration()` / conformal sets | Reliability diagrams + ECE on classifier confidences; split-conformal prediction sets | Calibration panel; claims carry coverage-guaranteed label sets | The UI already displays model confidences — this makes them *mean something*, with distribution-free guarantees at trivial cost |
| `suggest_labeling_batch(n)` | Active learning (uncertainty/diversity sampling) over unlabeled documents | — (feeds `dataset_mcp` labeling, not a panel) | Closes the loop: the platform chooses what to annotate next to improve its own models fastest |

**Adaptive-UI-facing ML (a distinct category).** Two candidates improve
the *generative UI itself* rather than rendering panels: a contextual
bandit ranking the empty-canvas suggestions and mover list by observed
click-through (Thompson sampling over the existing usage signals), and
panel-affinity recommendations (light matrix factorization over
pin/mute/touch history) feeding `apply_signals` priors. These consume the
adaptivity telemetry the canvas already collects, run entirely locally,
and need no new UI — the canvas just gets measurably better at guessing.
They should ship behind the same signals-reset control the UI already
has, and their effect must remain inspectable (the plan note says when a
learned prior influenced ranking — same honesty norm).

Wave-2 selection rules: prefer closed-form/frequentist methods with
scipy-grade dependencies; anything requiring a trained model must have a
lexicon/statistical fallback; every tool reports effect sizes with
uncertainty, never bare scores. Deliberately excluded for now: optimal
transport document distances, agent-based simulation, and deep temporal
KG models — cost/complexity out of proportion to mission value.

Integration rules (inherited from the rest of the plan):

- **Precompute heavy, serve light.** Fits run as batch jobs (existing
  `trigger_*` / APScheduler / Airflow patterns) writing result tables
  (like `outlet_scores` today); the MCP tools *read* results and only
  compute on-demand when cheap (<~1s on the warehouse). MLflow logs every
  fit for reproducibility.
- **Statistical honesty is part of the contract.** Tools must return
  sample sizes, intervals, and test assumptions in `outputSchema`; panels
  render them (no naked point estimates). The planner's note explains
  which analysis ran and on how much data — same transparency norm as
  "Hidden for now…".
- **Heuristic-fallback house style applies.** Every tool has a
  dependency-light statistical implementation (numpy/scipy/scikit-learn,
  already required); no GPU or network model needed for the default path.
- **Grounded planning composes.** A Stage-2 planner can chain: intent
  mentions "unusual" → `detect_anomalies` → if a window flags, add the
  claims/stance panels scoped to it. Track P composes too: every tool
  takes an optional KG namespace, so provisioned domains get the full
  analytics plane for free.

Sequencing: after Stage 1 (tools-as-panels is the delivery mechanism);
pairs naturally with Stage 2. Wave 1: start with `detect_anomalies` +
`score_confidence` (highest value, lowest risk), then `lead_lag` and
`cluster_narratives`; treat forecasting as the deliberate caboose.
Wave 2, by the same value ranking: `coverage_bias` and `burst_detect`
first (cheapest, sharpest), then `model_calibration` (retrofits meaning
onto confidences the UI already shows), `event_impact`,
`coordination_detect` (the differentiator, but needs careful
false-positive discipline — flag cohorts, never accuse), then the
text-profile family, with the adaptive-UI bandit as a parallel
experiment gated on the honesty norm.
*Effort: incremental per tool (that is the point). Risk: statistical
misuse at scale — mitigated by the honesty contract and precompute-first
rule; coordination findings additionally require conservative thresholds
and human review before any public-facing surface.*

## What deliberately does not change

- `ui-spec-v1` contract, validators, and fixtures.
- The heuristic planner and the browser's offline client planner.
- Frontend live/demo fallback semantics and the adaptive usage signals.
- WAF/JWT/RBAC middleware as the only externally reachable surface.
- The `.mcp.json` dev-tooling experience (dev agents keep using the same
  servers; they gain tools rather than losing any).

## Risks

| Risk | Mitigation |
|---|---|
| Server lifecycle complexity (12 child processes) | Pooled sessions, lazy connect, health endpoint in `/api/v1/ui/context`, static-catalog fallback |
| Latency regression on generate/data paths | Call budgets, discovery/stats caching (TTL ~60s), Stage-3 benchmark gate |
| Loose typing of tool results | Require `outputSchema` for panel-annotated tools; contract tests validate sample outputs |
| Auth story for remote MCP | Defer: local stdio only until a concrete remote need; proxy inherits existing HTTP auth |
| Scope creep into a data-plane rewrite | Stages are independently shippable; Stage 3 explicitly gated on measurement |

## Open questions

1. Tool→panel annotation format: FastMCP tags vs. a `meta.panel` block in
   tool descriptions — pick whichever survives `tools/list` serialization
   cleanly across FastMCP versions.
2. Where do bespoke renderers live long-term — keyed by tool name, or by
   a `renderer` hint the server declares?
3. Should domain-pack enable/disable move fully to "which servers are
   configured", retiring `config/domain_packs.json`, or keep the file as
   the source that *selects* servers? (Proposal: keep the file, it selects
   servers.)
4. Does Stage 3 use MCP resources (for cacheable reads) rather than tool
   calls for panel data?
5. Track P namespacing: per-KG DuckDB table prefixes (simple, plays well
   with the existing warehouse) vs. graph partitions in a real graph store
   (Neptune/Gremlin config exists but is not the local-first default).
   Proposal: table prefixes first; the namespace abstraction hides it.
6. Track P source criteria: how expressive should `kg_attach_sources`
   criteria be — a fixed filter schema over `sources_mcp` profile fields,
   or free predicates the agent evaluates itself? (Proposal: fixed schema;
   the agent can always pre-select and pass an explicit list.)

## Recommendation

Adopt Stages 0–2. They remove the duplication and drift that motivate the
rearchitecture and make the LLM planner meaningfully better, at bounded
risk, without touching the hot data path. Decide Stage 3 only after the
Stage-1 session infrastructure has soaked and a latency benchmark exists;
Stage 4 is cheap opportunistic surface once 1–2 land.

Track P (agent-provisioned KGs with source selection) is the end-state
that makes the rearchitecture strategic rather than cosmetic: capability
creation itself becomes an agent operation. Sequence it after Stage 1
lands (discovery is its delivery mechanism) and prototype it first as a
dry-run planner suggestion ("deploy a KG for X fed by these sources?")
before enabling real writes behind the approval gate.

Track DS is the cheapest way to keep shipping visible value while the
plumbing lands: each tool is an independent increment, the first two
(`detect_anomalies`, `score_confidence`) need nothing but Stage 1 and
libraries already in the stack, and every tool automatically benefits
Track P domains later.
