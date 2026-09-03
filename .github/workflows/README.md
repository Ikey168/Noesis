# Workflow inventory

Noesis keeps only runnable workflows in this directory. Superseded manual
copies and the old `disabled/` archive were removed in September 2026; Git
history remains the archive.

## Required product checks

- `unit-tests.yml`: pull-request and push gate for the lightweight capability
  plane, including KB, argument mining, ingestion, foundation KG, and MCP
  adapter contracts.
- `argument-model-benchmarks.yml`: sampled external benchmark gate on relevant
  pull requests, plus the pretrained comparison on Monday and on demand.
- `full-test-suite.yml`: weekly and manual full-dependency test run for test
  areas that are too expensive for the pull-request lane.
- `airflow-dag-check.yml`: Airflow DAG import and validation.
- `contracts-ci.yml`: schema compatibility checks.
- `dbt-duckdb.yml`, `dbt-spark.yml`, `semantic-layer-ci.yml`: analytical-model
  and semantic-layer checks.
- `mlops-ci.yml`: model-lifecycle checks.

## Operational workflows

- `ci-cd-pipeline.yml`: main application/container build and deployment lane.
- `containerized-tests.yml`: Docker connectivity and image smoke tests.
- `canary-deployment.yml`: intentionally manual production canary operation.
- `terraform.yml`: infrastructure validation.
- `test-news-api.yml`: API-key-backed News API integration check.

Operational workflows may require repository environments or secrets. The
manual canary is the only intentionally dispatch-only operational workflow;
all other manual entry points also have a scheduled or change-driven trigger.
