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
Track OSINT composes those into an **investigation plane** — corroboration,
entity dossiers, relationship paths, timeline reconstruction — bound by a
strict evidence discipline and a defensive-only scope (analysis over
already-ingested open sources, never active or targeted collection).
Track N carries the beyond-news continuation: research as the second
first-class domain pack, a de-newsed shared layer, further domains
(finance, legal, OSINT) arriving Track P-provisioned, and the alias-first
naming sweep.

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

*Delivered (phase 1, R8/R9): table-prefix namespacing, routing, and the
`kg_*` tool surface with guardrails, on a two-domain acceptance. Phase 2 (P2,
issues #640-#644) extends the same surface to orchestrate databases and
pipelines: a KG can deploy into its own attached DuckDB database (`backend`:
`table-prefix` or `attached`), bind a pipeline with `kg_attach_pipeline`
(contract-validated at attach), and `kg_ingest` runs the bound connectors
before routing (connector to contract to enrich to route), with quotas on
databases and pipelines and a teardown that detaches the database and unbinds
the connectors, never cascading to the shared corpus. Two domains stand up
with their own database and pipeline via provisioning alone;
`docs/provisioning-p2-acceptance.md` is the write-up. The one remaining
integration point is wiring the ingest runner to the real `pipeline_mcp`
connector execution.*

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

## Track OSINT — the investigation plane

OSINT (open-source intelligence *analysis*) is the application that most
of the platform was quietly building toward: entity resolution (M3),
provenance-carrying claims with SUPPORTS/CONTRADICTS (M6), the citation/
mention graph, source transparency scoring, and Track DS's coordination
detection and graph centrality compose directly into investigative
workflows — corroborating a claim across independent sources,
reconstructing a timeline, mapping who-connects-to-whom, spotting
coordinated narratives. Track OSINT packages these into an investigation
posture: a `research`-style domain pack (or Track-P-provisioned domain)
plus a few OSINT-specific tools and panels, and — the real deliverable —
a **provenance and corroboration discipline** the rest of the platform
already half-implements.

### Scope (read this before the tool list)

Track OSINT is **defensive/analytical only**: it reasons over documents
**already ingested** through the normal connector pipeline (news, blogs,
papers, transcripts, filings, public web the operator chose to collect).
It is an analysis layer, not a collection tool.

Out of scope, by design — enforced as the absence of tools, not a policy
note:

- **No active/targeted collection.** No scraping keyed to a named
  individual, no login-walled or private data, no purchased data brokers,
  no de-anonymization. Ingestion stays the general connector framework,
  which pulls *sources*, not *people*.
- **No surveillance of private persons.** Person entities are analyzed
  only as they appear in already-public documents; the unit of analysis
  is the *claim and its sourcing*, not a dossier on an individual.
- **No offensive/operational output.** No target packages, no
  location-tracking of individuals, no anything whose primary use is to
  act against a person. Geotemporal tools resolve *where an event was
  reported to occur*, from document content — not where a person is.

This scope is the mission: the same "hold sources accountable, show your
work" ethic as the transparency ranking, pointed at investigations.

### Tools (compose first, add sparingly)

Most OSINT value is composition of existing capabilities; only a few
genuinely new tools are needed.

| Tool | Built on | Panel | Role |
|---|---|---|---|
| `corroborate(claim_id)` | M6 claims + RAG + `source_scores` | Corroboration panel: independent sources for/against, weighted by source credibility | The core OSINT primitive — how many *independent* sources support this, and how good are they |
| `entity_dossier(entity_id)` | M3 resolution + KG + mentions | Entity brief: every public mention, resolved aliases, first/last seen, connected entities — all cited | A *cited* profile from public docs only; every line links to its source document |
| `relationship_path(a, b)` | KG shortest-path / centrality (Track DS) | Connection graph between two entities | "How is A connected to B" across the corpus, with the evidence on each edge |
| `timeline_reconstruct(topic|entity)` | claims + `created_at` + burst/changepoint (Track DS) | Evidence timeline with corroboration density per event | Reconstructs a sequence of events from dated, cited claims |
| `source_reliability(source)` | transparency machinery, generalized to any source_type | Reliability card (track record, corroboration hit-rate, correction history) | Extends outlet scoring into an OSINT-grade source-vetting signal |
| `narrative_coordination(topic)` | Track DS `coordination_detect` | Coordinated-cohort graph | Astroturf / influence-operation surfacing — flag cohorts, never accuse |
| `geolocate_claims(topic)` | NER (places) + geocoding of document *content* | Event map (where events are *reported* to occur) | Places extracted from text, not people tracked; strictly event-geography |
| `contradiction_scan(entity|topic)` | M6 CONTRADICTS edges | Contradiction ledger | Surfaces where the public record disagrees with itself |

### The non-negotiable: an evidence discipline

OSINT output that can't be traced is worse than useless. Track OSINT
tightens the honesty contract into an **evidentiary** one:

| Rule | Meaning |
|---|---|
| **Every assertion is a link.** | No claim, edge, or timeline entry renders without a citation to the source document + chunk (`path`). "Uncited" is a render state, flagged, never hidden. |
| **Corroboration is explicit.** | Panels show *independent-source count* and credibility, not a single confidence number. One source = clearly marked single-sourced. |
| **Confidence is calibrated, not asserted.** | Reuses Track DS calibration/conformal work; an entity match or geolocation carries an interval, and resolution ambiguity (M3) is surfaced, not resolved silently. |
| **Provenance chain is inspectable.** | `lineage_mcp` already models this — an investigation artifact can be traced source → connector → enrichment → claim → panel. |
| **Audit trail on RW actions.** | Any provisioning (a Track-P investigation KG) or export is logged; investigations are reconstructable. |
| **Person-entity guardrail.** | Tools operating on person entities require the person to appear in ≥1 ingested public document and surface only document-sourced facts; no inference-only claims about individuals. |

### Delivery

- As a **domain pack / MCP server** (`osint`), it rides Stage 1: its
  annotated tools become panels via discovery, gated by an `osint`
  `ui_flag`. No genui code changes.
- It **composes Track DS** (coordination, centrality, burst, calibration)
  and **Track P** (an investigation is naturally a provisioned,
  namespaced KG fed by a chosen source set — with the audit trail Track P
  already mandates).
- Empty-canvas telemetry, when the OSINT pack dominates, shifts from
  "trending topics" to "open threads / newly corroborated / newly
  contradicted".

Sequencing: after Stage 1 + the first Track DS tools (`corroborate`
needs source scoring and claims, both present; the rest lean on DS).
Start with `corroborate` + `source_reliability` + `contradiction_scan`
(pure composition, highest value, lowest risk), then `entity_dossier` /
`relationship_path`, with `geolocate_claims` and `narrative_coordination`
last and behind the strictest review — they are the most abusable and the
most false-positive-prone.
*Effort: mostly composition; medium. Risk: **misuse and false
confidence** — mitigated by the scope boundary (analysis over ingested
open sources only, enforced by which tools exist) and the evidence
discipline above. This track's guardrails are load-bearing, not
decorative.*

## Track N — beyond news: the domain-plurality plan

The knowledge-engine pivot (`KNOWLEDGE_ENGINE_PIVOT_PLAN.md`) delivered a
domain-general *engine* (Document contract, connectors for
papers/books/blogs/media/upload, KG foundation with ontology + entity
resolution + provenance, claim extraction — all shipped, see its §15.1).
What remains news-centric is the *product posture* (its §15.3): the news
pack is the only first-class pack, the canvas's ambient telemetry
(movers, KPI tiles, BREAKING ticker) is news telemetry regardless of
corpus mix, the transparency machinery is worded and stored for
newsrooms (`outlet_*`), and the `NEURONEWS_*` brand residue pervades
infra. Track N is the continuation plan, folded in here because every
step lands on this document's machinery. This is the **canonical home**
of the plan; the pivot doc points here.

### N1 — Research pack: the second first-class domain
A `research` DomainPack / MCP server proving pack-plurality (the papers
milestone locked this domain choice). Enrichers over paper metadata;
`ui_flags` for new panel types — `citation_graph`, `venues` (per-venue
credibility, reusing the transparency machinery), `literature_claims`
(SUPPORTS/CONTRADICTS from the claim layer) — plus research-flavored
empty-canvas telemetry (recently ingested papers, emerging concepts)
when the pack dominates the corpus mix.
*Depends on: nothing here — can start now on the static catalog; rides
Stage 1 discovery when it lands. Effort: medium.*

### N2 — De-news the shared layer
Generalize outlet→source naming behind views (keep `outlet_*` tables,
add `source_*` views); make empty-canvas telemetry pack-supplied
(movers/ticker come from whichever packs are enabled, not hardcoded news
hooks); anchor overview-panel availability on `documents` rather than
`news_articles`.
*Pairs with: Stage 1 (availability from stats tools makes the anchor
swap natural). Effort: small–medium, mostly mechanical.*

### N3 — New domains arrive provisioned, not hand-built
Finance (earnings-call transcripts — the media connector already
transcribes), legal/policy, and **OSINT/investigation** (Track OSINT)
arrive as Track P-provisioned domains: `kg_deploy` + `kg_attach_sources`
instead of a pack directory. Standing one of these up without writing a
pack **is Track P's acceptance test**. OSINT is the sharpest fit — it is
mostly composition of capabilities already built, under Track OSINT's
evidence discipline.
*Depends on: Track P (and Stage 1). Effort: per domain, small once
Track P exists — that is the point.*

### N4 — Naming/alias sweep
`NOESIS_*` env vars with `NEURONEWS_*` fallbacks, package
(`@neuronews/web`) and MCP server (`neuronews-*`) renames, API/page
titles. Mechanical, alias-first so nothing breaks, and deliberately
last — after N1–N3 make Noesis demonstrably not-a-news-app.
*Depends on: nothing technically; sequenced last by policy. Effort:
small, wide.*

Sequencing summary: N1 now → N2 with Stage 1 → N3 as Track P's exit
criterion → N4 cleanup.

## Milestones (proposed sequence)

The stages and tracks above define scope; this is the dependency-ordered
delivery sequence, in the same style as the pivot plan's M0–M12 (each
independently shippable, each with an exit criterion).

### Foundation

**R0 — Catalog codegen** *(Stage 0)*
`spec.ts` types/catalog and the contract enums generated from
`catalog.py`; CI staleness check.
*Exit:* editing `catalog.py` without regenerating fails CI; the
hand-written mirrors are deleted.

**R1 — MCP host runtime**
Pooled, supervised FastMCP sessions inside the API process: lazy
connect, health checks, discovery cache (TTL), per-server status in
`GET /api/v1/ui/context`. No planning behavior changes yet — this is
the risky infrastructure, isolated.
*Exit:* all 12 servers connected with health visible; killing a server
is reflected within one TTL; API boot time within ±10% of today.

**R2 — Discovery-derived catalog** *(Stage 1)*
Decide the tool→panel annotation format (open question 1), annotate the
existing servers' panel-shaped tools, and merge discovered `PanelDef`s
over the static catalog (which remains the fallback).
*Exit:* dropping in a new annotated server surfaces a panel type in
`/api/v1/ui/panels` with zero genui changes; with every server down the
canvas is byte-identical to today.

**R3 — Adaptivity from tools** *(Stage 1 + N2)*
`merged_ui_flags`/availability from server presence + stats tools
(DuckDB probe demoted to fallback); overview availability re-anchored
on `documents`; empty-canvas telemetry becomes pack-supplied.
*Exit:* `/api/v1/ui/context` availability is tool-sourced when servers
are up; a corpus with zero news still gets a live overview canvas.

### Intelligence

**R4 — Grounded LLM planning** *(Stage 2)*
Bounded tool-use loop (allowlist, call/time budgets, stats caching);
output still sanitized, validated, signal-enforced.
*Exit:* with a mocked MCP client, plans demonstrably skip empty-data
panels; p95 generate latency with the loop stays within an agreed
budget.

*Delivered.* The loop lives in `src/genui/llm.py`: with the host up the
LLM planner may call a curated read-only inspection allowlist
(`am_stats`, `article_stats`, `document_stats`, the `list_*` / `*_stats`
tools — never the RW `trigger_*`/`run_*`/`compute_*` tools) for up to
`MAX_TOOL_ROUNDS` (3) rounds, then emits the spec. **Latency budget:**
`NOESIS_GENUI_LOOP_BUDGET_MS` (default 9000 ms) bounds the whole loop's
wall-clock; when the remaining budget drops below `MIN_LOOP_BUDGET_MS`
(1500 ms) at the start, or the deadline is crossed mid-loop, the planner
degrades to a single one-shot completion (the pre-R4 path) rather than
issuing more turns. Tool results are served through the host's shared
`call_tool_cached` (per `(server, tool, args)`, TTL
`NOESIS_MCP_STATS_TTL`, default 60 s, invalidated on reconnect), which
the adaptivity layer also reads — so two consecutive generates trigger at
most one live round per stats tool. Kill switch: `NOESIS_GENUI_LOOP=off`.

**R5 — Analytics foundation + first tools** *(Track DS Wave 1a)*
The batch-fit pattern (trigger tools / scheduler → result tables →
MLflow) plus `detect_anomalies` and `score_confidence` with
`outputSchema` carrying n / intervals / assumptions.
*Exit:* anomaly and confidence-interval panels render from precomputed
tables via R2 discovery; no tool output ships without its uncertainty
fields.

*Delivered.* The analytics plane lives in `src/analytics/` (stdlib-only
maths — no numpy/scipy needed, so the tool servers stay import-safe):
`honesty.py` is the statistical-honesty contract (`n` / `method` /
`assumptions` required, headline figures carry intervals,
`validate_analytic_output` is the gate); `framework.py` is the
`AnalyticJob` + `run_job` batch pattern (result table + optional MLflow,
which degrades to a warning when absent); `anomalies.py` is the reference
analytic (robust z-score over per-topic daily volume/sentiment) with an
`AnomalyJob` fit and a `detect_anomalies` read tool; `confidence.py` adds
`score_confidence` (bootstrap CI) and `stance_significance` (permutation
test). Tools: `trigger_detect_anomalies` + `detect_anomalies`
(pipeline_mcp, annotated for the `anomaly_timeline` panel),
`score_confidence` + `stance_significance` (argument_mcp). The
`anomaly_timeline` panel type + error bars on `outlet_ranking` render the
uncertainty fields; the tools read precomputed tables and the panel data
path lands on the MCP data proxy at R12 (demo data until then).

**R6 — Analytics breadth** *(Track DS Wave 1b, then Wave 2 rolling)*
`lead_lag`, `cluster_narratives`, `kg_communities`/`centrality`,
`semantic_drift`; `forecast_topic` last.

*Delivered.* `src/analytics/` grew `lead_lag.py` (cross-correlation
lead-lag, the `lead_lag` panel; smallest-magnitude lag wins on ties),
`narratives.py` (bag-of-words cosine clustering, the `narrative_thread`
panel), `graph.py` + `kg_analytics.py` (pure-Python PageRank +
label-propagation communities feeding the community-coloured
`entity_graph`), and `drift.py` (`semantic_drift` → `drift_trajectory`
panel; `forecast_topic` → `forecast` panel via Holt smoothing, always
banded). Tools: `lead_lag`, `cluster_narratives`, `semantic_drift`,
`forecast_topic` (pipeline_mcp, annotated for the four new panels),
`kg_communities` / `kg_centrality` (kg_mcp, no panel — they enrich the
entity graph, and take an optional `kg` namespace for Track P). Everything
honesty-wrapped; `forecast_topic` never returns a point without its
interval. Wave 2 tools (coverage bias,
burst, calibration, …) then land as independent per-tool increments —
no milestone gate each.
*Exit:* "who leads on X" plans a lead-lag matrix panel end-to-end.

### Expansion

**R7 — Research pack** *(Track N1)*
The second first-class domain: `research` pack/server with
`citation_graph`, `venues`, `literature_claims` panels and
research-flavored telemetry.
*Exit:* with research on and news off, the canvas is fully functional
on research panels; venue credibility renders via the generalized
transparency machinery.

*Delivered.* `src/domains/research/` is the second first-class pack,
built the same way as news: paper-metadata enrichers (venue, citations,
concept), `ui_flags` gating the research panel family, and
`research_telemetry` (recent papers, emerging concepts) surfaced when the
pack dominates. `tools/research_mcp/` exposes `venues`
(credibility as a composite of concept diversity, attribution and
citation impact, generalizing outlet transparency scoring, honesty-
wrapped), `citation_graph` (paper citation network) and
`literature_claims` (claims scoped to papers), annotated for the three
new panels and surfaced through R2 discovery. The pack is registered in
`.mcp.json` and at API/domain-pack startup; enable it with
`NEURONEWS_ENABLED_PACKS=research`. Verified end-to-end with research on
and news off: the three panels discover and plan, venue credibility
carries a CI, telemetry becomes NEW PAPERS, and a generic briefing still
gets a live overview.

**R8 — Provisioning plane** *(Track P)*
`kg_deploy` / `kg_attach_sources` / `kg_ingest` / `kg_status` /
`kg_teardown` with table-prefix namespacing, lineage registration,
quotas, approval gates, idempotent upserts.
*Exit:* deploy → attach by criteria → ingest → scoped panels appear via
discovery; teardown archives; every step visible in lineage.

*Delivered.* The provisioning plane lives in `src/provisioning/` (stdlib-only;
the caller injects the DuckDB connection, so nothing here opens the warehouse
itself): `namespaces.py` is the table-prefix namespacing (`kg_<name>_documents`
/ `_entities` / `_claims`, name validated to `[a-z][a-z0-9_]*` before it ever
reaches a table identifier) plus the routing that copies only the rows whose
source is bound to a KG out of the shared corpus, leaving the shared tables
untouched; `store.py` is the registry and an append-only lineage event log,
every write an idempotent upsert keyed by name; `guardrails.py` is the quotas
(max KGs, max sources per KG, ingest rate cap), the deploy/teardown approval
gate and the confirm gate; `provisioner.py` ties them into deploy / attach /
ingest / status / list / teardown, resolving quality criteria (transparency,
attribution, type) against `outlet_scores`. Served by
`tools/provisioning_mcp/` (write tools hold a process lock over a read-write
warehouse, mirroring the pipeline server's trigger tools; read tools open
read-only). The `provisioned_kg` panel type surfaces the plane on the canvas
via R2 discovery (the `kg_view` tool is annotated). Teardown archives (renames
the namespace tables aside, never deletes) and detaches sources; re-running a
failed provision converges without duplicating.

**R9 — Provisioned-domain proof** *(Track N3 = Track P acceptance)*
Stand up finance (earnings-call transcripts) as a provisioned domain,
writing no pack code; repeat with a second domain to prove it wasn't a
fluke.
*Exit:* two domains live via provisioning alone.

*Delivered.* Two domains stand up over the real `provisioning_mcp` server
with no pack code: `finance` (earnings-call transcripts, bound by explicit
source list) and `legal` (policy filings, bound by a `min_transparency`
criterion resolved against the outlet scores), each deploy to ingested in
roughly 250 ms. Each namespace holds only its own routed documents (the
shared corpus is read, never written) and exposes a scoped
documents/entities/claims family through `kg_view(kg)`, surfaced on the
canvas by the discovered `provisioned_kg` panel. The one R8 friction found,
that `kg_view` returned counts without the scoped sample, is fixed in the
provisioning plane (`namespaces.namespace_sample` + `Provisioner.view`), not
with pack code. The executable harness is `scripts/provisioning/acceptance.py`,
the regression is `tests/unit/provisioning/test_acceptance.py`, and the
write-up with the friction list is `docs/provisioning-acceptance.md`.

**R10 — OSINT composition** *(Track OSINT, phase 1)*
`corroborate`, `source_reliability`, `contradiction_scan` under the
evidence discipline (citations mandatory, single-source flagged,
calibrated confidence).
*Exit:* a corroboration panel shows independent-source counts weighted
by credibility; uncited assertions render visibly flagged, never
hidden.

*Delivered.* The OSINT plane lives in `src/osint/` (stdlib-only, pure
composition of the claim / evidence / conflict / outlet-score layers; the
connection is injected read-only): `corroboration.py` is `corroborate(claim_id)`,
counting the independent sources that support or contradict a claim (by
distinct outlet, excluding the claim's own source) each weighted by its
transparency composite, flagging a claim as `single_sourced` rather than
inventing a confidence number; `reliability.py` is `source_reliability(source)`,
the outlet transparency score generalized to any source_type plus a
corroboration hit-rate and a disputed-claim rate, honesty-wrapped with a
track-record-weighted interval; `contradictions.py` is
`contradiction_scan(topic|entity)` over the CONTRADICTS edges, every pair
joined back to both sources and citations with uncited entries flagged, never
dropped. Served by `tools/osint_mcp/` (read-only, 15 project servers), each
tool annotated for the `corroboration` / `reliability_card` /
`contradiction_ledger` panels surfaced via R2 discovery under the `osint`
`ui_flag`.

**R11 — OSINT investigation surface** *(Track OSINT, phase 2)*
`entity_dossier`, `relationship_path`, `timeline_reconstruct`;
"investigation" formalized as a provisioned KG with audit trail;
person-entity guardrails enforced in the tools. `geolocate_claims` and
`narrative_coordination` stay behind an explicit review gate and may
ship later or not at all.
*Exit:* an entity brief where every line links to its source document;
guardrail tests prove person-tools refuse non-document-sourced facts.

*Delivered.* The investigation surface extends `src/osint/` (stdlib-only,
pure composition over `document_actors` / `argument_claims` /
`news_articles`; the connection is injected read-only): `evidence.py` is the
shared evidence discipline (a citation on every line, the cited /
single_sourced / uncited render states); `dossier.py` is
`entity_dossier(entity)`, a cited brief (mentions, aliases, first/last seen,
connected entities) with the person-entity guardrail enforced in code, a
person with no ingested document is refused rather than described from
inference; `paths.py` is `relationship_path(a, b)`, the shortest co-mention
path with the establishing documents cited on every edge and resolution
ambiguity surfaced; `timeline.py` is `timeline_reconstruct(topic|entity)`,
dated cited claims bucketed into events each carrying its corroboration
density. `investigations.py` formalizes an investigation as a Track
P-provisioned KG reconstructable from its provisioning audit trail
(`investigation_audit`), supplies the OSINT-dominant empty-canvas telemetry
(open threads / newly corroborated / newly contradicted), and names the
review-gated `geolocate_claims` / `narrative_coordination` (absent from the
served surface, enforced by a test; gate in `docs/osint-review-gate.md`).
Served by `tools/osint_mcp/` as the `entity_dossier`, `relationship_path` and
`evidence_timeline` panels via R2 discovery under the `osint` `ui_flag`.

### Ecosystem

**R12 — Data-plane benchmark and Stage 3 decision** *(Stage 3 gate)*
Data-mode tool variants, a `/api/v1/ui/data` proxy prototype, and a
latency benchmark against the equivalent REST routes; go/no-go recorded
as an ADR.
*Exit:* the decision is written down with numbers; if go, one panel
family serves via the proxy at production parity.

*Delivered.* The `articles_data` data-mode tool (pipeline server, `meta.data`
block) returns the full `articles` payload, byte-identical to
`/api/v1/news/articles`. The `POST /api/v1/ui/data` proxy
(`src/genui/dataplane.py` + `src/api/routes/genui_data_routes.py`, behind the
`NOESIS_GENUI_DATA_PROXY` flag) invokes only allowlisted data-mode tools,
rate-limited per client and size-capped both ways. The benchmark
(`scripts/genui/dataplane_benchmark.py`) measured, on a 200-row payload:
`rest_direct` 13.3 ms p50, `mcp_tool` 46.1 ms, `proxy_cold` 50.4 ms,
`proxy_cached` 0.36 ms, all at the same 50,743-byte payload. Decision (recorded
in `docs/architecture/ADR-002-data-plane-stage3.md`): **conditional go**,
promote the `articles` family behind the flag cache-served (proxy_cached beats
REST), do not move cold first-loads onto the proxy until the transport overhead
(~3.8x today) closes; retirement criteria documented. The frontend renders the
articles panel through the proxy when the flag reports enabled, falling back to
demo otherwise.

**R13 — Noesis as MCP server + naming sweep** *(Stage 4 + N4)*
`noesis_generate_view` over Streamable HTTP for external hosts;
`NOESIS_*` env aliases (`NEURONEWS_*` fallbacks), package/server/API
title renames.
*Exit:* an external MCP host generates and receives a valid
`ui-spec-v1`; both env prefixes verified working.

*Delivered.* `tools/noesis_mcp/server.py` exposes Noesis as an MCP server:
`noesis_generate_view(intent)` returns a validated `ui-spec-v1` (reusing the
heuristic planner and pack `ui_flags`) and `noesis_panels()` returns the
catalog, over stdio or Streamable HTTP (`NOESIS_MCP_TRANSPORT=http`), with an
optional `NOESIS_MCP_AUTH_TOKEN` gate (auth story in
`docs/noesis-mcp-server.md`). Verified live: an external FastMCP client over
HTTP generated and received a valid spec. `src/config/env.py` is the one shared
resolver (`NOESIS_X`, else `NEURONEWS_X`), applied at the config surface
(enabled packs, warehouse path across the canvas tool servers and the shared
connector); both prefixes resolve identically (tested). The user-facing
surfaces are renamed to Noesis (web package `@noesis/web`, page and API titles,
API root message); the retained `neuronews-*` MCP server names, `NEURONEWS_*`
env prefix and on-disk warehouse filename survive as documented aliases
(`docs/naming.md`).

**Critical path:** R0 → R1 → R2 → R3 → R8 → R9. The analytics/OSINT
chain (R5 → R6 → R10 → R11) runs in parallel after R2; R4 upgrades
quality anywhere after R2; R7 can start immediately on the static
catalog and re-lands on discovery at R2; R12–R13 are opportunistic once
R1–R2 have soaked.

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

The milestone sequence R0–R13 above is the executable form of all of
this: R0–R3 are the commitment-light foundation, everything after is a
choice made with working infrastructure underneath it.
