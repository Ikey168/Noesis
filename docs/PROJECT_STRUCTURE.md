# Project Structure

How the Noesis repository is laid out. For setup and the tech stack, see the
[root README](../README.md); for the documentation map, see
[`docs/index.md`](index.md).

## Application code

| Path | Contents |
|---|---|
| `src/` | Python backend. Notable packages: `api/` (FastAPI app and routes), `argument_mining/` (claim/stance/frame models), `nlp/`, `knowledge_graph/`, `ingestion/`, `scraper/`, `ml/`, `security/`, `monitoring/`, `reports/`, `domains/`. |
| `services/` | Standalone services: `api/`, `rag/`, `embeddings/`, `mlops/`, `ingest/`, `monitoring/`, `obs/`, `metrics-api/`. |
| `tools/` | Token-efficient MCP stdio servers for development (`argument_mcp/`, `pipeline_mcp/`, `contract_mcp/`, `lineage_mcp/`, `kg_mcp/`, `blog_mcp/`, `schema_mcp/`, `dataset_mcp/`, `domain_packs_mcp/`, `sources_mcp/`, `security_mcp/`, `monitoring_mcp/`). |
| `connectors/` | News/data source connector definitions. |

## Data, contracts & pipelines

| Path | Contents |
|---|---|
| `contracts/` | Data contracts, schemas, evolution policies, and validation tooling. |
| `dbt/` | dbt project for warehouse transformations. |
| `spark/` | Spark jobs (streaming, Iceberg). |
| `jobs/` | Batch/streaming job definitions. |
| `data/`, `test_data/` | Local datasets and fixtures. |
| `data_quality/` | Data-quality checks and expectations. |
| `migrations/`, `db/` | Database migrations and schema. |
| `models/` | Trained model checkpoints (gitignored when absent; heuristics used as fallback). |
| `evals/` | Evaluation harnesses and results. |

## Orchestration & infrastructure

| Path | Contents |
|---|---|
| `airflow/` | Airflow DAGs and operators. |
| `marquez/` | OpenLineage / Marquez configuration. |
| `deploy/`, `deployment/` | Deployment manifests and scripts. |
| `infra/`, `infrastructure/` | Infrastructure as code. |
| `k8s/` | Kubernetes manifests. |
| `docker/` | Dockerfiles and compose configs. |
| `monitoring/`, `grafana/` | Monitoring stack and Grafana dashboards. |
| `config/`, `configs/` | Application and service configuration. |
| `scripts/` | Automation and utility scripts. |

## Tests, docs & examples

| Path | Contents |
|---|---|
| `tests/` | Unit, integration, and end-to-end tests. |
| `docs/` | Documentation (see [`docs/index.md`](index.md)). |
| `docs/architecture/` | System architecture and design, with diagrams ([overview](architecture/overview.md), [adaptive scraping](architecture/adaptive-scraping.md), ADRs, plans). |
| `docs/guides/` | Operational and integration guides. |
| `docs/development/` | Development deep-dives. |
| `docs/mlops/`, `docs/rag/`, `docs/lakehouse/` | Subsystem reference docs. |
| `docs/examples/`, `docs/notebooks/` | Runnable examples and notebooks. |
| `docs/archive/` | Historical docs (Snowflake/Redshift era, removed UI, per-issue writeups, old demos). |
| `archive/`, `artifacts/` | Archived material and build artifacts. |

## Root files

`README.md`, `Makefile`, `pytest.ini`, `requirements*.txt` (split by extra:
`-dbt`, `-embeddings`, `-qdrant`, `-snowflake`, `-vector`), and
`docker-compose.lineage.yml`.
</content>
