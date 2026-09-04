# NeuroNews Pipeline-stage runner (MCP server)

A thin local MCP server that exposes each ETL pipeline boundary as a typed tool,
so you can run **one stage against a fixture/source** and inspect a **summary or
diff** — never the full payload. It drives the project's own pipeline code
(`src/ingestion/scrapy_integration.py`) and the local docker-compose stack
(DuckDB warehouse, Postgres/pgvector, S3/MinIO).

## Tools

| Tool | What it does |
|---|---|
| `list_sources(category?)` | List the configured RSS connectors (`DEFAULT_FEEDS`), optionally filtered by category. |
| `run_connector(source, sample=5)` | Fetch + parse up to `sample` articles from one source's **live** feed. Summary only (count, date range, sentiment distribution, sample titles). No write. |
| `run_stage(stage, input_ref?, sample=5, apply=false)` | Run one stage: `fetch` (source→article refs), `sentiment` (text or article id → score/label), `store` (fetch→**diff** vs warehouse; `apply=true` to insert), `ingest` (full fetch→store). |
| `trace_article(id)` | Where an article id exists across the **warehouse** (read-only row summary), **pgvector**, and **S3/MinIO** (reachability). |

## Run / register

Registered in the repo's `.mcp.json` as a stdio server (`noesis-pipeline`),
launched from the repo root:

```bash
python3 tools/pipeline_mcp/server.py
```

Requires `fastmcp` (`pip install fastmcp`). `psycopg2` and `boto3` are optional —
used best-effort for the pgvector/S3 checks in `trace_article`.

## Design constraints (why it's shaped this way)

- **Lazy imports.** The module top imports only stdlib + `fastmcp`, so the
  server starts instantly and never triggers the repo's heavy ML import graph
  (transformers/torch), which can hang on import. Pipeline code is imported
  inside each tool.
- **Read-only warehouse.** DuckDB is single-writer. The server opens the
  warehouse **read-only** for inspection so it doesn't fight a running
  API/ingester's writer lock, and reports the lock as a clean status string
  rather than crashing. Writes (`store`/`ingest` with `apply=true`) need the
  warehouse free — point `NOESIS_DB_PATH` at a free file if the API is up.
- **Summaries, not payloads.** Lists are capped (`MAX_LIST`) and article content
  is truncated (`CONTENT_PREVIEW`).
- **Best-effort backends.** pgvector/S3 checks use short timeouts; an
  unreachable backend yields `{"reachable": false}`, never a hang.

## Environment

| Var | Default | Used by |
|---|---|---|
| `NOESIS_DB_PATH` | `<repo>/data/neuronews.duckdb` | warehouse reads/writes |
| `PGVECTOR_HOST` / `PGVECTOR_PORT` | `localhost` / `5433` | `trace_article` vector check |
| `PGVECTOR_DSN` | `postgresql://neuronews:…@localhost:5433/neuronews_vector` | `trace_article` |
| `S3_ENDPOINT_URL` / `AWS_ENDPOINT_URL` | unset | `trace_article` object-store check |
