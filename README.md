![Airflow DAG Check](https://github.com/Ikey168/Noesis/actions/workflows/airflow-dag-check.yml/badge.svg)
![MLflow CI](https://github.com/Ikey168/Noesis/actions/workflows/mlops-ci.yml/badge.svg)

# Noesis: the verifiable evidence layer for agents

Noesis (formerly NeuroNews) ingests documents (news, blogs, papers,
transcripts, books, filings, media), mines arguments and evidence from them,
and exposes everything as a **capability plane** for agents. Every factual line
has a source locator or remains visibly `uncited`; corroboration is a count of
independent sources; model outputs identify their `prediction_mode`; and every
inferential headline number carries an interval plus `n`, method, and
assumptions. Those are executable contracts, not presentation conventions.

The same evidence discipline works over public feeds and a private corpus kept
on your own machine. Content-addressed snapshots, silent-edit detection, image
reuse, C2PA status, and cross-modal contradictions form one integrity ledger,
so an agent can check both what a source says and whether the record changed.

The capability plane is not just for reading. Agents provision new knowledge
domains: they stand up namespaced knowledge graphs, select and attach the
sources that feed them, and run the pipelines — all through the same tool
surface. The full design lives in the
[MCP rearchitecture plan](docs/architecture/mcp-rearchitecture.md).

> **Consuming Noesis from another project?** See
> [docs/integration/mcp-and-api.md](docs/integration/mcp-and-api.md) for the MCP server
> list, the REST API surface, auth, and example tool calls.
> To verify the receipts offline, run `make evidence-showcase`; see the
> [evidence showcase](docs/guides/evidence-showcase.md).

---

## The three pillars

1. **MCP capability plane.** Subsystem MCP servers expose the platform:
   ingestion, argument mining, evidence and OSINT, the knowledge graph,
   statistics, provisioning, research, lineage, security, and more. Every
   subsystem is a tool server; external hosts (Claude Desktop, another agent,
   your own service) discover and call the tools directly.
2. **REST API.** The same subsystems are reachable over HTTP (`src/api/`) for
   clients that prefer REST to MCP — documents, argument mining, the knowledge
   graph, evidence, search, reports, and more.
3. **Agent-provisioned knowledge.** An audited agent host runs analyst and
   investigator agents over the provisioning and OSINT planes. Agents deploy
   knowledge graphs with their own storage and pipelines under quota and
   approval guardrails, and every run is budgeted, allowlisted, and recorded as
   a replayable transcript.

---

## What it does

- **Argument mining.** Detects claims, classifies stances, identifies frames
  (economic, security, humanitarian, legal, political, scientific, other),
  extracts actor and entity mentions, and tracks how policy positions evolve.
- **Fact-check and corroboration.** Links claims to verdicts, scores
  corroboration by independent-source count, flags unsourced assertions, and
  keeps a contradiction ledger of where the public record disagrees with itself.
- **Private corpus, local first.** Applies the same claim, contradiction,
  provenance, and diff surfaces to PDFs, DOCX, email, books, filings, notes,
  and transcripts without uploading them to a hosted service. See the
  [private-corpus quickstart](docs/guides/private-corpus.md).
- **Integrity ledger.** Unifies cited snapshots, silent corrections, image
  reuse, C2PA content credentials, and prose-versus-figure checks behind one
  MCP/REST/KB surface.
- **Portable evidence bundles.** Exports answers, claims, integrity records,
  and receipts as content-addressed `noesis-evidence-bundle-v1` packages that
  recipients can verify offline without a warehouse or network connection.
- **Verifiable answers.** `kb_answer` turns one question into a deterministic
  `noesis-answer-v1` evidence plan: every extractive statement separates
  supporting and contradicting citations or refuses explicitly when evidence
  is insufficient. Python, MCP, and REST share the same implementation.
- **Claim Watches.** Durable claim, entity, topic, and saved-query watches emit
  immutable evidence-change events after committed consolidation watermarks.
  Owner-bound polling resumes through opaque cursors without gaps or duplicate
  logical events; private domains require explicit grants.
- **Evidence Independence Graph.** Corroboration keeps publication count
  separate from probable reporting origins, preserves unknown provenance, and
  exposes the signals behind syndication and dependency inference. Existing
  warehouses retain an explicit distinct-source fallback until backfilled.
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
| Backend | FastAPI, uvicorn |
| Analytics warehouse | DuckDB (local file, single-writer) |
| Argument mining | Pinned ClaimBuster and DeBERTa NLI models, distilbert, scikit-learn, spaCy |
| Scraping | Scrapy, Playwright, Selenium |
| Orchestration | Apache Airflow |
| MLOps | MLflow |
| Vector search | Qdrant, PostgreSQL with pgvector |
| Object storage | S3-compatible (MinIO) |
| Streaming | Kafka |
| Capability plane | FastMCP servers (stdio and Streamable HTTP) |

### Local-first

Every external service has a localhost default. Environment variables use the
`NOESIS_` prefix. Deprecated `NEURONEWS_` aliases remain available with runtime
warnings until Noesis 2.0 (not before 2027-09-01). Set them to point at managed
equivalents in production:

```text
NOESIS_DB_PATH         data/local_warehouse.duckdb   # DuckDB warehouse path
S3_ENDPOINT_URL        http://localhost:9000          # MinIO
DYNAMODB_ENDPOINT_URL  http://localhost:8000          # DynamoDB Local
```

### MCP capability plane

Every subsystem is a FastMCP server. Read tools run against the warehouse and
never conflict with the API writer, so they are safe for both development
agents and external hosts. Domain packs arrive as connected servers, and
provisioning tools stand up new knowledge graphs at runtime.

| Server | Focus |
|---|---|
| `noesis-catalog` | Permission- and readiness-filtered discovery across every registered capability |
| `noesis-pipeline` | Connectors, ingestion stages, article stats, and analytics |
| `noesis-arguments` | Claims, stances, frames, actors, outlet clustering and scoring |
| `noesis-osint` | Corroboration, reliability, dossiers, paths, timelines, and provenance |
| `noesis-kg` | Knowledge-graph stats, entities, communities, centrality, and corrections |
| `noesis-provisioning` | Lifecycle management for namespaced knowledge graphs |
| `noesis-research` | Citation graph, venue credibility, and literature claims |
| `noesis-sources` | Source profiles, trustworthiness, comparison, and outlet clusters |
| `noesis-domain-packs` | Enable, disable, run enrichers, and get UI flags |
| `noesis-blog-feeds` | Subscribe, ingest, and harvest Atom and RSS watchlists |
| `noesis-contracts` | List, get, and validate data contracts |
| `noesis-lineage` | Namespaces, nodes, lineage, impact, and run history |
| `noesis-dataset` | Training-dataset stats, schema, label distribution, and sampling |
| `noesis-monitoring` | Current and historical metrics and summaries |
| `noesis-security` | Security posture, secret and TLS checks, backups, and DB permissions |
| `noesis-schema` | Tables, schemas, and REST routes |
| `noesis-kb` | Versioned KB search, answers, evidence, diffs, integrity, and briefs |

---

## Getting started

### 1. Clone and create an environment

```bash
git clone https://github.com/Ikey168/Noesis.git
cd Noesis
python -m venv .venv
source .venv/bin/activate
```

### 2. Install the local-first CLI

```bash
python -m pip install -e ".[minimal]"
noesis init --non-interactive
noesis doctor
```

This creates a private local DuckDB workspace. It does not require Docker,
cloud services, an API key, or a model download.

### 3. Ingest, ask, and verify

```bash
noesis ingest examples/quickstart/moon-mission.md --domain local
noesis ask "What was the mission result?" --domain local
noesis export answer \
  --domain local \
  --question "What was the mission result?" \
  --include-private \
  --output answer.bundle.json
noesis verify answer.bundle.json
```

See the [CLI guide](docs/guides/cli.md) for Claim Watches, JSON output, server
launchers, configuration, and optional dependency groups.

### 4. Run a supported server surface

```bash
python -m pip install -e ".[server]"
noesis serve --surface api
# or: noesis serve --surface kb-mcp --transport http
```

### 5. Run tests

```bash
pytest                                        # unit and integration tests
```

Advanced model, service, scraper, and Docker commands remain supported and are
documented in [legacy and advanced entry points](docs/development/legacy-entry-points.md).

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
  its source types, enrichers, sources and provisioning templates, and the
  registry keeps immutable versions. A pack extends the platform without
  editing core code.
- **Agents** run over two planes (provisioning, OSINT) through a runtime that
  budgets tokens, enforces an allowlist, and audits every tool call. Runs can
  dispatch in-process or against the live MCP host, and each produces a
  replayable transcript.
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

For a complete offline policy-change example—including revision detection,
origin-aware corroboration, an authorized stale-guidance watch, and a verified
evidence bundle—run `make policy-monitor`. See the
[policy monitor tutorial](docs/guides/policy-monitor.md).

Provisioned knowledge graphs live in namespaced tables (and optionally their
own attached DuckDB databases), so a new domain never collides with the news
corpus.

---

## Model benchmarks (current defaults)

| Model | F1 | Notes |
|---|---|---|
| ClaimDetector | 0.9197 | Pinned ClaimBuster backend; external F1: FEVER 0.8038, LIAR 0.9418, AVeriTeC 0.9305 |
| StanceClassifier | 0.3288 macro | Pinned zero-shot NLI; promotion remains blocked on the real human gold set |
| FrameClassifier | 0.4193 macro | Zero-shot NLI; political and humanitarian recall remain weak |

See [docs/subsystems/argument-mining-benchmarks.md](docs/subsystems/argument-mining-benchmarks.md) for the full breakdown
by source type, length, external dataset, and per-class metrics. The
internal six-source test set is synthetic and is labelled as such; it is not a
substitute for the pending human evaluation. Run `make models` before inference;
when weights are absent the pipeline fails closed with an actionable error.

---

## Documentation

- [Documentation index](docs/index.md): full doc map by topic
- [Integrate via MCP + API](docs/integration/mcp-and-api.md): consuming Noesis from another project
- [MCP rearchitecture plan](docs/architecture/mcp-rearchitecture.md): capability plane and agent-provisioned knowledge graphs
- [Project structure](docs/development/project-structure.md)
- [Model benchmarks](docs/subsystems/argument-mining-benchmarks.md)
- [Exactly-once delivery design](docs/architecture/exactly-once-delivery.md)

---

## Roadmap

Phases 1 through 6 (scraping and ingestion; NLP, sentiment, and knowledge
graph; event detection and summarisation; dashboards and REST API; the
argument-mining pipeline; outlet analysis) are complete.

- **Phase 7, generative adaptive UI.** Shipped, then retired: Noesis now
  exposes its capabilities purely through the MCP servers and REST API, and
  the bespoke frontend has been removed.
- **Phase 8, MCP as the capability plane.** Complete. Every subsystem is an MCP
  tool server, plus the data-science analytics tools.
- **Phase 9, agent-provisioned knowledge.** Complete. Multi-tenant
  provisioning of namespaced knowledge graphs with their own storage and
  pipelines, and the audited agent host that drives them.
- **Phase 10, beyond news.** Complete. The research domain pack, finance and
  legal domains stood up through provisioning, and the OSINT investigation
  surface under evidence discipline.
- **Upcoming.** A two-annotator human gold set and a stance model trained on
  it; predictive analytics; live panel data on by default. Cross-dataset claim
  generalisation on FEVER, LIAR, and AVeriTeC is now published in the benchmark
  report.

---

## Contact and contributions

- GitHub Issues: bug reports and feature requests
- Pull Requests: contributions welcome
- Email: ikey168@proton.me
- License: MIT
