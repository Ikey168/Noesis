# Noesis Documentation

Noesis (formerly NeuroNews) is a generative knowledge engine. It ingests
documents (news, blogs, papers, transcripts), mines arguments and evidence from
them, and exposes everything through two surfaces: a generative UI whose every
screen is planned at runtime from a natural-language intent, and an MCP
capability plane whose subsystem tool servers the UI and agents compose against.

For a project overview, the tech stack, and local setup, start with the
[root README](../README.md). This index links the deeper reference docs.

## Generative UI and the capability plane

- [Generative UI](genui.md): the canvas, the heuristic and LLM planners,
  adaptivity, and the `ui-spec-v1` contract
- [MCP rearchitecture plan](architecture/MCP_REARCHITECTURE_PLAN.md): the
  capability plane and agent-provisioned knowledge graphs
- [Knowledge-engine pivot plan](architecture/KNOWLEDGE_ENGINE_PIVOT_PLAN.md):
  the claim and triple-centric knowledge-graph design

## Project reference

- [Project structure](PROJECT_STRUCTURE.md): directory layout and where things live
- [Security](security.md): API hardening, WAF, and auth notes
- [Model benchmarks](model_benchmarks.md): argument-mining model metrics

## Architecture and design

- [Exactly-once delivery design](EXACTLY_ONCE_DESIGN.md): streaming delivery guarantees
- [Data lineage naming](lineage_naming.md): OpenLineage namespace and naming conventions

## Guides

- [AWS deployment](guides/AWS_DEPLOYMENT_GUIDE.md)
- [CI/CD with Ansible](guides/CICD_ANSIBLE_INTEGRATION_GUIDE.md)
- [Lambda scraper automation](guides/LAMBDA_SCRAPER_AUTOMATION_GUIDE.md)
- [Monitoring system](guides/MONITORING_SYSTEM_GUIDE.md)
- [Anti-detection scraping](guides/ANTI_DETECTION_GUIDE.md)
- [Python integration](guides/PYTHON_INTEGRATION_GUIDE.md)
- [Dashboard deployment](DASHBOARD_DEPLOYMENT.md)

## RAG and retrieval

- [Quickstart](rag/quickstart.md)
- [Evaluation framework](rag/evaluation.md)
- [Qdrant and pgvector parity](rag/qdrant_parity.md)

## MLOps

- [Experiment tracking](mlops/experiments.md)
- [Model registry](mlops/model_registry.md)
- [Reproducibility framework](mlops/reproducibility_framework.md)
- [MLflow security](mlops/security.md)

## Lakehouse and streaming

- [Spark and Iceberg integration](lakehouse/spark-iceberg-integration.md)
- [Kafka to Spark to Iceberg streaming](lakehouse/kafka_spark_iceberg_streaming.md)
- [Enrichment upsert and merge](lakehouse/enrichment_upsert_merge.md)
- [Streaming backfill](streaming-backfill.md)

## Data warehouse (dbt and Snowflake)

- [dbt quickstart](dbt_quickstart.md)
- [Incremental strategy](incremental_strategy.md)
- [dbt and Snowflake parity](dbt_snowflake_parity.md)
- [Snowflake analytics integration](SNOWFLAKE_ANALYTICS_INTEGRATION.md)
- [Snowflake ETL implementation](SNOWFLAKE_ETL_IMPLEMENTATION.md)
- [Snowflake migration guide](SNOWFLAKE_MIGRATION_GUIDE.md)
- [Redshift removal](REDSHIFT_REMOVAL_DOCUMENTATION.md): legacy warehouse decommission notes

## Development notes

- [Graph-based search](development/GRAPH_BASED_SEARCH_IMPLEMENTATION.md)
- [Iceberg maintenance](development/ICEBERG_MAINTENANCE_IMPLEMENTATION.md)
- [OpenLineage and Marquez](development/OPENLINEAGE_MARQUEZ_IMPLEMENTATION.md)

## Feature implementation notes

Per-feature implementation writeups live in
[`implementation/`](implementation/):

- AI summarization, sentiment analysis, fake-news detection, keyword and topic
  extraction, multi-language and multi-source ingestion
- Knowledge-graph population, async pipeline, data validation, monitoring
- S3 storage, DynamoDB metadata, and Redshift ETL

## Runnable examples

- [`examples/`](examples/): tutorials and ML demos
- [`demo/`](demo/): standalone feature demo scripts
- [`notebooks/`](notebooks/): Jupyter notebooks
