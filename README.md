![Airflow DAG Check](https://github.com/Ikey168/Noesis/actions/workflows/airflow-dag-check.yml/badge.svg)
![MLflow CI](https://github.com/Ikey168/Noesis/actions/workflows/mlops-ci.yml/badge.svg)

# Noesis: Investigation Engine

Noesis (formerly NeuroNews) investigates questions against an ingested public
record. It ingests documents (news, blogs, papers, transcripts), mines claims
and evidence from them, and then does what a knowledge base cannot: it takes
a **question**, holds **competing hypotheses** against the record, pursues
**leads**, weighs cited evidence by independent-source corroboration, and
either reaches a verdict or refuses to and names exactly what is missing.

Everything is exposed through two surfaces:

- a **generative UI**, where there are no fixed pages: every screen is a
  layout planned at runtime from a natural-language intent, adapted to the
  data that actually exists, the enabled knowledge domains, and the operator's
  habits;
- an **MCP capability plane**, where every subsystem is a tool server that the
  UI, development agents, and autonomous agents compose against.

The capability plane is not just for reading. Agents open and drive
investigations, and provision new knowledge domains: they stand up namespaced
knowledge graphs, select and attach the sources that feed them, run the
pipelines, and the UI grows panels for those domains through tool discovery
alone. The full design lives in the
[MCP rearchitecture plan](docs/architecture/MCP_REARCHITECTURE_PLAN.md).

---

## The four pillars

1. **Investigation engine.** A case is a durable object: a question, at least
   two competing hypotheses (every case can state its own disconfirmation),
   replayable leads over the OSINT layer, cited credibility-weighted evidence,
   and an append-only journal. The engine plans leads, pursues them, scores
   the hypothesis matrix ACH-style, and concludes only through an
   evidence-discipline gate - enough independent sources, a real margin, no
   unanswered contradiction, no open leads - otherwise the case stays open
   with its gaps named. See [docs/investigations.md](docs/investigations.md).
2. **Generative canvas.** The frontend has no fixed views. Each screen is a
   `ui-spec-v1` document planned from an intent ("compare outlet framing on
   climate policy"), validated against a contract, and rendered from a registry
   of 43 panel types built on a small set of reusable chart primitives. The
   only control is a command bar; the planner runs as you type.
3. **MCP capability plane.** Seventeen subsystem MCP servers expose the
   platform: the investigation engine, ingestion, argument mining, evidence
   and OSINT, the knowledge graph, provisioning, research, lineage, security,
   and more. The panel catalog, the panel data, and the LLM planner are all
   grounded in tool discovery and tool calls.
4. **Agent-provisioned knowledge.** An audited agent host runs analyst and
   investigator agents over the provisioning, OSINT, and generative-UI planes.
   Agents deploy knowledge graphs with their own storage and pipelines under
   quota and approval guardrails, and every run is budgeted, allowlisted, and
   recorded as a replayable transcript.

---

## What it does

- **Case work.** Open a case on a question (`POST /api/v1/investigation/run`
  or the `noesis-investigation` MCP tools) and the engine corroborates every
  claim that speaks to it, scans for contradictions, reconstructs the
  timeline, pulls entity dossiers and connection paths, vets every source its
  own evidence introduced, and delivers a cited brief: a verdict when the
  evidence-discipline gate passes, the named gaps when it does not. Counter-
  claims are recognized through the conflict graph, so corroboration of an
  opposing claim counts with its direction flipped.
- **Adaptive generative UI.** Every screen is planned from an intent as a
  validated `ui-spec-v1` document (heuristically, or by an LLM when a key is
  configured) and adapted to warehouse data availability, installed domain
  packs, and the operator's pins and mutes. Canvases can be saved, reopened,
  refined in place, and shared by read-only link. See [docs/genui.md](docs/genui.md).
- **Argument mining.** Detects claims, classifies stances, identifies frames
  (economic, security, humanitarian, legal, political, scientific, other),
  extracts actor and entity mentions, and tracks how policy positions evolve.
- **Fact-check and corroboration.** Links claims to verdicts, scores
  corroboration by independent-source count, flags unsourced assertions, and
  keeps a contradiction ledger of where the public record disagrees with itself.
- **OSINT investigation surface.** Entity dossiers, relationship paths,
  reconstructed timelines, and provenance traces over ingested open sources,
  all under a strict evidence discipline (every line is cited; uncited entries
  are flagged, never hidden). Sensitive tools stay off by default behind a flag.
- **Source transparency.** Scores every outlet by framing diversity, claim
  attribution rate, and stance neutrality, generalized to any source type, with
  weekly snapshots and sparkline history.
- **Data-science analytics as tools.** Anomaly detection, lead-lag, narrative
  clustering, coverage forecasting, semantic drift, graph science, and
  significance testing are planner-composable tools that report honest
  uncertainty (confidence intervals and prediction bands, never a bare point).
- **Domain packs.** Knowledge domains are installable `noesis-pack-v1`
  manifests with their own source types, enrichers, planner vocabulary, and
  panels. A research pack (citation graph, venue credibility, literature
  claims) is a first-class second domain; finance and legal domains are stood
  up through provisioning alone.
- **Ingestion.** Scrapy spiders with Playwright and Selenium rendering for
  JavaScript-heavy pages, plus Atom and RSS blog watchlists, feeding a
  contract-validated pipeline.
- **NLP and knowledge graph.** Named-entity extraction, sentiment scoring,
  keyword trends, and knowledge-graph linking with community and centrality
  analysis.

---

## Architecture

### Stack

| Layer | Technology |
|---|---|
| Frontend | React 18, Vite, TypeScript, TanStack Query, Tailwind CSS with shadcn/ui |
| Backend | FastAPI, uvicorn |
| Analytics warehouse | DuckDB (local file, single-writer) |
| Argument mining | distilbert with heuristic fallback, scikit-learn, spaCy |
| Scraping | Scrapy, Playwright, Selenium |
| Orchestration | Apache Airflow |
| MLOps | MLflow |
| Vector search | Qdrant, PostgreSQL with pgvector |
| Object storage | S3-compatible (MinIO) |
| Streaming | Kafka |
| Capability plane | FastMCP servers (stdio and Streamable HTTP) |

### Local-first

Every external service has a localhost default. Environment variables use the
`NOESIS_` prefix, with `NEURONEWS_` accepted as a fallback for continuity. Set
them to point at managed equivalents in production:

```text
NOESIS_DB_PATH         data/local_warehouse.duckdb   # DuckDB warehouse path
S3_ENDPOINT_URL        http://localhost:9000          # MinIO
DYNAMODB_ENDPOINT_URL  http://localhost:8000          # DynamoDB Local
NEPTUNE_ENDPOINT       ws://localhost:8182/gremlin
```

### MCP capability plane

Every subsystem is a FastMCP server. Read tools run against the warehouse and
never conflict with the API writer, so they are safe for both development
agents and the live UI. The generative-UI panel catalog is derived from tool
discovery, domain packs arrive as connected servers, and provisioning tools
stand up new knowledge graphs at runtime.

| Server | Focus |
|---|---|
| `investigation_mcp` | The investigation engine: open, run, advance and conclude cases; case files, ACH hypothesis matrices, cited briefs |
| `pipeline_mcp` | Connectors, ingestion stages, article stats, and the analytics tools (anomaly, lead-lag, narratives, forecast, drift, sentiment, positions, conflicts) |
| `argument_mcp` | Claims, stances, frames, actors, outlet clustering and scoring, stance drift, benchmarks |
| `osint_mcp` | Corroboration, contradiction scan, source reliability, entity dossier, relationship path, timeline reconstruction, provenance trace, investigation audit |
| `kg_mcp` | Knowledge-graph stats, entities, communities, centrality, corrections, evolving topics |
| `provisioning_mcp` | Deploy, attach sources and pipelines, ingest, status, list, view, teardown, lineage for namespaced knowledge graphs |
| `research_mcp` | Citation graph, venue credibility, literature claims |
| `sources_mcp` | Source profiles, trustworthiness, comparison, outlet clusters |
| `domain_packs_mcp` | Enable, disable, run enrichers, get UI flags |
| `blog_mcp` | Subscribe, ingest, and harvest Atom and RSS watchlists |
| `contract_mcp` | List, get, and validate data contracts |
| `lineage_mcp` | Namespaces, nodes, lineage, impact, run history |
| `dataset_mcp` | Training-dataset stats, schema, label distribution, sampling |
| `monitoring_mcp` | Current and historical metrics and summaries |
| `security_mcp` | Security posture, secret and TLS checks, backups, DB permissions |
| `schema_mcp` | Tables, schemas, routes, hooks, mock exports |
| `noesis_mcp` | External-facing generate-view server over Streamable HTTP |

---

## Getting started

### 1. Clone

```bash
git clone https://github.com/Ikey168/Noesis.git
cd Noesis
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
cd apps/web && npm install && cd ../..
```

### 3. Run the API

```bash
NEURONEWS_DEV_MODE=true \
NOESIS_DB_PATH=/tmp/noesis-dev.duckdb \
uvicorn src.api.app:app --port 8012
```

`NEURONEWS_DEV_MODE=true` disables the WAF so development requests are not
rejected. Use a separate `NOESIS_DB_PATH` to avoid locking the main warehouse
file.

### 4. Run the frontend

```bash
cd apps/web
npm run dev          # http://localhost:5173
```

The React app falls back to bundled demo data when the API is unreachable, so
the canvas works standalone for UI development.

### 5. Run tests

```bash
pytest                                        # unit and integration tests
npx tsc --noEmit -p apps/web/tsconfig.json    # TypeScript type check
python scripts/genui/codegen.py --check       # generated genui artifacts are current
```

### 6. Other entry points

```bash
# Argument-mining model benchmarks and the merge gate
python scripts/benchmark_models.py
python scripts/benchmark_models.py --gate

# Train models (falls back to heuristics when a checkpoint is absent)
python -m src.argument_mining.train_claim  --data data/argument_mining

# Scraper
python -m src.scraper.run --spider bbc

# Docker
docker compose up --build
```

---

## Generative canvas

The frontend has no fixed views. Each screen is a canvas: a `ui-spec-v1`
layout generated from an intent by `POST /api/v1/ui/generate` (or by a
client-side planner when the backend is unreachable) and rendered from a
registry of 43 panel types. Panels span articles and library documents,
trending and event clusters, sentiment and claims, framing and stance, actor
positions and conflicts, outlet ranking and clustering, the entity graph, the
research family, provisioned knowledge graphs, and the OSINT surface.

Panels are built on seven reusable chart primitives in
`apps/web/src/components/charts/`: `LineBand` (line with a confidence band),
`TimelineAxis` (dated event axis), `Sankey` (layered flow), `BarKit` (grouped
and diverging bars), plus `Heatmap`, `EntityGraph`, and `Sparkline`. Each
primitive serves several panels across different data layers.

The single control is a command bar: the planner runs as you type, showing
parsed intent tokens and a live preview of the layout before you commit it. An
empty canvas shows the live pipeline signal instead of a greeting. Layouts
adapt to warehouse data availability, enabled domain packs, and the operator's
pins, mutes, and interaction history. Canvases persist locally, can be refined
in place with a follow-up instruction, and can be shared by read-only link.
See [docs/genui.md](docs/genui.md).

---

## Agents, packs, and provisioning

These surfaces ship behind feature flags that are off by default:

| Flag | Enables |
|---|---|
| `NOESIS_GENUI_LLM` | LLM planner (grounded, tool-using) instead of the heuristic planner |
| `NOESIS_GENUI_DATA_PROXY` | Live panel data over MCP through the `POST /api/v1/ui/data` proxy |
| `NOESIS_PACKS_ADMIN` | Pack install and publish routes |
| `NOESIS_AGENT_API` | Analyst and investigator agent routes |
| `NOESIS_AGENT_TRANSPORT` | `local` (in-process) or `live` (real MCP host) for agent runs |
| `NOESIS_OSINT_GATED_TOOLS` | Sensitive OSINT tools, kept off unless explicitly enabled |
| `NOESIS_ENABLED_PACKS` | Domain packs to load at startup |

- **Domain packs** are installable `noesis-pack-v1` manifests: a pack declares
  its source types, enrichers, planner keywords, UI flags, and panels, and the
  registry keeps immutable versions. A pack surfaces its panels and vocabulary
  without editing the core catalog.
- **Agents** run over three planes (provisioning, OSINT, generative UI) through
  a runtime that budgets tokens, enforces an allowlist, and audits every tool
  call. Runs can dispatch in-process or against the live MCP host, and each
  produces a replayable transcript.
- **Provisioning** lets an agent deploy a namespaced knowledge graph with its
  own attached storage and pipelines, bounded by quotas and an approval gate,
  with idempotent upserts and lineage. Finance and legal domains are stood up
  this way, with no core code changes.

---

## Key warehouse tables

| Table | Contains |
|---|---|
| `news_articles` | Ingested articles and metadata |
| `argument_claims` | Detected claims with attribution and fact-check verdicts |
| `source_stances` | Per-source stance aggregations by topic |
| `stance_drift_events` | Detected stance reversals |
| `document_frames` | Per-document frame scores (seven dimensions) |
| `document_actors` | Actor and entity mentions extracted from documents |
| `policy_positions` | Extracted actor policy stances |
| `claim_conflicts` | Claim-versus-claim contradiction records |
| `outlet_clusters` | k-means and hierarchical cluster assignments |
| `outlet_scores` | Weekly transparency scores (diversity, attribution, neutrality) |
| `investigations` | Case records: question, hypotheses scope, status, verdict |
| `investigation_evidence` | Cited, credibility-weighted evidence rows per case |
| `investigation_leads` | Planned and pursued leads (replayable tool calls) |
| `investigation_events` | The append-only case journal |

Provisioned knowledge graphs live in namespaced tables (and optionally their
own attached DuckDB databases), so a new domain never collides with the news
corpus.

---

## Model benchmarks (heuristic baseline)

| Model | F1 | Notes |
|---|---|---|
| ClaimDetector | 0.8645 | Binary; blog and transcript are the weakest source types |
| StanceClassifier | 0.4506 macro | Neutral class dominates; minority stances underperform |
| FrameClassifier | 0.5200 macro | Political frame recall is near zero in heuristic mode |

See [docs/model_benchmarks.md](docs/model_benchmarks.md) for the full breakdown
by source type, length, and per-class metrics. When a trained checkpoint is
absent the pipeline falls back to keyword heuristics and still returns valid
predictions.

---

## Documentation

- [Documentation index](docs/index.md): full doc map by topic
- [Investigation engine](docs/investigations.md): cases, hypotheses, leads, the evidence-discipline gate
- [Generative UI](docs/genui.md): the canvas, planners, adaptivity, ui-spec-v1
- [MCP rearchitecture plan](docs/architecture/MCP_REARCHITECTURE_PLAN.md): capability plane and agent-provisioned knowledge graphs
- [Project structure](docs/PROJECT_STRUCTURE.md)
- [Model benchmarks](docs/model_benchmarks.md)
- [Exactly-once delivery design](docs/EXACTLY_ONCE_DESIGN.md)

---

## Roadmap

Phases 1 through 6 (scraping and ingestion; NLP, sentiment, and knowledge
graph; event detection and summarisation; dashboards and REST API; the
argument-mining pipeline; outlet analysis) are complete.

- **Phase 7, fully generative adaptive UI.** Complete. Fixed views replaced by
  the intent-planned canvas (`ui-spec-v1`, heuristic and optional LLM planner,
  a command bar with live plan preview, usage-signal adaptivity).
- **Phase 8, MCP as the capability plane.** Complete. Catalog from discovery,
  grounded LLM planning, MCP-backed panel data, Noesis as an MCP server, and
  the data-science analytics tools.
- **Phase 9, agent-provisioned knowledge.** Complete. Multi-tenant
  provisioning of namespaced knowledge graphs with their own storage and
  pipelines, and the audited agent host that drives them.
- **Phase 10, beyond news.** Complete. The research domain pack, finance and
  legal domains stood up through provisioning, and the OSINT investigation
  surface under evidence discipline.
- **Phase 11, the investigation engine.** Complete. Cases as first-class
  durable objects (question, competing hypotheses, leads, cited evidence,
  journal), the plan/pursue/evaluate loop over the OSINT layer, ACH hypothesis
  matrices under the honesty envelope, the evidence-discipline conclusion
  gate, and the case brief - over HTTP and MCP.
- **Upcoming.** Trained model checkpoints; cross-dataset generalisation
  (FEVER, LIAR, AVeriTeC); predictive analytics; live panel data on by default;
  investigation canvas panels (case board, hypothesis matrix, brief).

---

## Contact and contributions

- GitHub Issues: bug reports and feature requests
- Pull Requests: contributions welcome
- Email: ikey168@proton.me
- License: MIT
