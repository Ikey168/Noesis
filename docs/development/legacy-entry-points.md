# Advanced and legacy entry points

The root quickstart is intentionally the installable `noesis` interface.
Existing Python modules, REST and MCP servers, infrastructure recipes, and
environment aliases remain supported for automation that already uses them.

Run `make legacy-help` to discover the retained Airflow, dbt, MLflow, vector,
container, model, and contract-management recipes. These are advanced platform
operations, not prerequisites for a private local corpus.

`NOESIS_*` is the canonical environment namespace. `NEURONEWS_*` aliases are
deprecated but remain accepted for the complete 1.x release series. Removal
requires a 2.0 contract change, a release-note notice, and at least one minor
release that emits actionable warnings. Existing Python, REST, and MCP entry
points follow the same policy.
