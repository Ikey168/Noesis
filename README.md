![Airflow DAG Check](https://github.com/Ikey168/NeuroNews/actions/workflows/airflow-dag-check.yml/badge.svg)
![MLflow CI](https://github.com/Ikey168/NeuroNews/actions/workflows/mlops-ci.yml/badge.svg)

# Noesis — Generative Knowledge Engine

Noesis (formerly NeuroNews) is a knowledge engine that ingests articles,
blog posts, papers, and transcripts, mines arguments from them, and surfaces
everything through a **fully generative UI**: there are no fixed pages —
every screen is a layout planned at runtime from a natural-language intent,
adapted to the data that actually exists, the enabled knowledge domains, and
the operator's habits.

The architecture is converging on **MCP as the capability plane**: the
platform's subsystems are already exposed as MCP servers, and the
[rearchitecture plan](docs/architecture/MCP_REARCHITECTURE_PLAN.md) takes
this to its end state — agents that don't just compose views over existing
knowledge, but **provision new knowledge domains**: deploying knowledge
graphs and selecting the sources that feed them, with the UI growing panels
for new domains through tool discovery alone.

---

## What it does

- **Adaptive generative UI** — the entire frontend is a generative canvas:
  there are no fixed views. Every screen is planned from a natural-language
  intent ("compare outlet framing on climate policy") as a validated
  `ui-spec-v1` document — heuristically or by an LLM when a key is
  configured — and adapted to warehouse data availability, domain packs,
  and the operator's pins/mutes. The sidebar only manages open canvases —
  all navigation is the prompt. See [docs/genui.md](docs/genui.md).
- **Argument mining** — detects claims, classifies stances, identifies frames
  (economic / security / humanitarian / legal / political / scientific / other),
  extracts actor/entity mentions, and tracks how policy positions evolve over
  time.
- **Source transparency ranking** — scores every outlet by framing diversity,
  claim attribution rate, and stance neutrality; publishes a weekly snapshot
  with sparkline history.
- **Outlet clustering** — groups sources by editorial framing using k-means +
  Ward hierarchical clustering, with a PCA 2-D scatter plot.
- **Conflict graph** — visualises claim conflicts and contradictions between
  sources.
- **Fact-checking integration** — links claims to external verdicts; flags
  unsourced assertions.
- **Blog / feed ingestion** — subscribes to Atom/RSS watchlists and ingests
  matching posts into the pipeline.
- **News scraping** — Scrapy-based spiders with Playwright/Selenium rendering
  for JavaScript-heavy pages.
- **NLP & sentiment** — named-entity extraction, sentiment scoring, keyword
  trends, knowledge-graph linking.

---

## Architecture

### Stack

| Layer | Technology |
|---|---|
| Frontend | React 18, Vite, TypeScript, React Query, Tailwind CSS + shadcn/ui |
| Backend | FastAPI, uvicorn |
| Analytics warehouse | DuckDB (local file; single-writer) |
| Argument mining | distilbert / heuristic fallback, scikit-learn, spaCy |
| Scraping | Scrapy, Playwright, Selenium |
| Orchestration | Apache Airflow |
| MLOps | MLflow |
| Vector search | Qdrant, PostgreSQL/pgvector |
| Object storage | S3-compatible (MinIO) |
| Metadata store | DynamoDB-compatible |
| Streaming | Kafka (`localhost:9092` default) |

### Local-first

Every external service has a localhost default. Set environment variables to
point at managed equivalents in production:

```
S3_ENDPOINT_URL        http://localhost:9000     # MinIO
DYNAMODB_ENDPOINT_URL  http://localhost:8000     # DynamoDB Local
NEPTUNE_ENDPOINT       ws://localhost:8182/gremlin
NEURONEWS_DB_PATH      data/local_warehouse.duckdb  # DuckDB warehouse path
```

### MCP servers

Every subsystem is exposed as a FastMCP stdio server. Today they serve
development agents (token-efficient, read-only against the warehouse so
they never conflict with the API writer) — and, per the
[MCP rearchitecture plan](docs/architecture/MCP_REARCHITECTURE_PLAN.md),
they are the basis of the future capability plane: the generative-UI panel
catalog derived from tool discovery, domain packs as connected servers,
LLM planning grounded in tool calls, and provisioning tools
(`kg_deploy` / `kg_attach_sources`) that stand up new knowledge graphs at
runtime.

| Server | Tools |
|---|---|
| `tools/argument_mcp/` | `am_stats`, `list_claims`, `list_stances`, `list_drift_events`, `claim_evidence_pairs`, `list_unsourced_claims`, `trigger_attribution_batch`, `list_actors`, `actor_summary`, `trigger_actor_batch`, `list_outlet_clusters`, `trigger_outlet_clustering`, `list_outlet_scores`, `trigger_outlet_scoring`, `get_benchmark_results` |
| `tools/pipeline_mcp/` | `list_sources`, `run_connector`, `run_stage`, `trace_article` |
| `tools/contract_mcp/` | `list_contracts`, `get_contract`, `validate` |
| `tools/lineage_mcp/` | `list_namespaces`, `list_nodes`, `lineage`, `impact`, `run_history` |
| `tools/domain_packs_mcp/` | `list`, `enable`, `disable`, `run_enrichers`, `get_ui_flags` |
| `tools/blog_mcp/` | `subscribe_feed`, `ingest_feeds`, `run_watchlist`, `harvest_feed` |
| `tools/schema_mcp/` | `list_tables`, `get_schema`, `list_routes`, `get_route` |
| `tools/dataset_mcp/` | `get_stats`, `get_schema`, `label_distribution`, `sample_examples`, `check_criteria` |

---

## Getting started

### 1. Clone

```bash
git clone https://github.com/Ikey168/NeuroNews.git
cd NeuroNews
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Install frontend dependencies

```bash
cd apps/web && npm install && cd ../..
```

### 4. Run the API

```bash
NEURONEWS_DEV_MODE=true \
NEURONEWS_DB_PATH=/tmp/neuronews-dev.duckdb \
uvicorn src.api.app:app --port 8012
```

`NEURONEWS_DEV_MODE=true` disables the WAF so development requests are not
rejected. Use a separate `NEURONEWS_DB_PATH` to avoid locking the main
warehouse file.

### 5. Run the frontend

```bash
cd apps/web
npm run dev          # http://localhost:5173
```

The React app falls back to bundled mock data when the API is unreachable, so
the dashboard works standalone for UI development.

### 6. Run tests

```bash
pytest                      # unit + integration tests
npx tsc --noEmit -p apps/web/tsconfig.json   # TypeScript type check
```

### 7. Evaluate argument mining models

```bash
# Evaluate ClaimDetector, StanceClassifier, FrameClassifier on test split
python scripts/benchmark_models.py

# Enforce the ≥2 pp F1 improvement gate before merging a new checkpoint
python scripts/benchmark_models.py --gate

# With external benchmark datasets (optional)
python scripts/benchmark_models.py --fever /data/fever/ --liar /data/liar/
```

Results are written to `docs/benchmark_results.json` and
`docs/model_benchmarks.md`.

### 8. Train argument mining models

```bash
# Requires data/argument_mining/{claims,stance,frames}.parquet (issue #109)
python -m src.argument_mining.train_claim  --data data/argument_mining
python -m src.argument_mining.train_stance --data data/argument_mining
python -m src.argument_mining.train_frames --data data/argument_mining
```

Models are saved to `models/{claim_detector,stance_classifier,frame_classifier}/`.
When a trained checkpoint is absent the pipeline falls back to keyword heuristics
and still returns valid predictions.

### 9. Run the scraper

```bash
python -m src.scraper.run --help
python -m src.scraper.run --spider bbc
python -m src.scraper.run --multi-source
```

### 10. Docker (optional)

```bash
docker compose up --build
docker compose -f docker-compose.test-minimal.yml up --build --abort-on-container-exit
```

---

## Generative canvas

The frontend has no fixed views. Each screen is a **canvas**: a `ui-spec-v1`
layout generated from an intent by `POST /api/v1/ui/generate` (or by a
client-side planner when the backend is unreachable) and rendered from a
registry of ~20 panel types — articles, library documents, trending, event
clusters, sentiment heatmap, entity graph, claims, stance, framing, actor
positions, conflicts, stance drift, outlet ranking/clusters, watchlist,
story timeline, and more.

The single control is a ⌘K command bar: the planner runs as you type,
showing parsed intent tokens and a live ghost of the layout before ⏎
commits it. An empty canvas shows the live pipeline signal (entity
constellation, moving topics that generate coverage views) instead of a
greeting. The sidebar only manages open canvases (persisted in
localStorage). The surface is built with Tailwind + shadcn/ui. Layouts adapt to warehouse data availability, enabled
domain packs, and the operator's pins/mutes/interaction history. See
[docs/genui.md](docs/genui.md).

---

## Key warehouse tables

| Table | Contains |
|---|---|
| `news_articles` | Ingested articles and metadata |
| `argument_claims` | Detected claims with attribution and fact-check verdicts |
| `source_stances` | Per-source stance aggregations by topic |
| `stance_drift_events` | Detected stance reversals |
| `document_frames` | Per-document frame scores (7 dimensions) |
| `document_actors` | Actor/entity mentions extracted from documents |
| `policy_positions` | Extracted actor policy stances |
| `position_updates` | Tracked changes to policy positions |
| `claim_conflicts` | Claim-vs-claim contradiction records |
| `outlet_clusters` | k-means / hierarchical cluster assignments |
| `outlet_scores` | Weekly transparency scores (diversity / attribution / neutrality) |

---

## Model benchmarks (heuristic baseline)

| Model | F1 | Notes |
|---|---|---|
| ClaimDetector | 0.8645 | Binary; blog and transcript are the weakest source types |
| StanceClassifier | 0.4506 macro | Neutral class dominates; minority stances underperform |
| FrameClassifier | 0.5200 macro | Political frame recall is near zero in heuristic mode |

See [`docs/model_benchmarks.md`](docs/model_benchmarks.md) for full breakdown
by source type, article length, and per-class metrics.

---

## Documentation

- [Documentation index](docs/index.md) — full doc map by topic
- [Generative UI](docs/genui.md) — the canvas, planners, adaptivity, ui-spec-v1
- [MCP rearchitecture plan](docs/architecture/MCP_REARCHITECTURE_PLAN.md) — capability plane + agent-provisioned knowledge graphs
- [Project structure](docs/PROJECT_STRUCTURE.md)
- [Model benchmarks](docs/model_benchmarks.md)
- [Exactly-once delivery design](docs/EXACTLY_ONCE_DESIGN.md)

---

## Roadmap

- Phase 1: Web scraping and data ingestion — complete
- Phase 2: NLP, sentiment analysis, and knowledge graph — complete
- Phase 3: Event detection and AI summarisation — complete
- Phase 4: Interactive dashboards and REST API — complete
- Phase 5: Argument mining pipeline (claims, stances, frames, positions, conflicts, actors) — complete
- Phase 6: Outlet analysis (clustering, transparency scoring, conflict graph) — complete
- Phase 7: Fully generative adaptive UI — fixed views replaced by the
  intent-planned canvas (`ui-spec-v1`, heuristic + optional LLM planner,
  ⌘K command bar with live plan preview, usage-signal adaptivity) — complete
- Phase 8: MCP rearchitecture — MCP as the capability plane
  ([plan](docs/architecture/MCP_REARCHITECTURE_PLAN.md), staged):
  catalog-from-discovery, grounded LLM planning, MCP-backed panel data,
  Noesis-as-MCP-server, **Track P** — agent-provisioned knowledge graphs
  with quality-driven source selection — and **Track DS** — data-science
  techniques (anomaly detection, lead-lag, narrative clustering, graph
  science, significance testing) as planner-composable tools — proposed
- Phase 9: Beyond news — research domain pack (citation graph, venue
  credibility, literature claims) as the second first-class pack;
  finance/legal and an **OSINT / investigation** domain (corroboration,
  entity dossiers, relationship paths, timeline reconstruction — defensive
  analysis over ingested open sources, under a strict evidence discipline)
  provisioned via Track P; pack-aware canvas telemetry and outlet→source
  generalisation
  ([Track N](docs/architecture/MCP_REARCHITECTURE_PLAN.md)) — proposed
- Also upcoming: trained model checkpoints; cross-dataset generalisation
  (FEVER / LIAR / AVeriTeC); predictive analytics; real-time fact-checking

---

## Contact and contributions

- GitHub Issues: bug reports and feature requests
- Pull Requests: contributions welcome — see CONTRIBUTING.md
- Email: ikey168@proton.me
- License: MIT
