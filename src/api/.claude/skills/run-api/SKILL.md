---
name: run-api
description: Run, launch, start, or smoke-test the NeuroNews FastAPI backend (src/api/app.py). Use when asked to start the API server, curl an endpoint, check /health or /docs, verify a route returns data, or stand up the backend that feeds the web frontend (articles, trending, clusters, breaking news, sentiment).
---

# Run the NeuroNews FastAPI backend

`src/api/app.py` is the NeuroNews REST API (FastAPI + uvicorn). It serves
articles, trending topics, event clusters, breaking news and sentiment from a
**local DuckDB warehouse** that it creates and seeds with sample news on first
request — no AWS, no Snowflake, no network. This is the backend the web
frontend (`apps/web`) proxies to.

The agent path is the smoke driver at
`src/api/.claude/skills/run-api/smoke.sh`: it launches the API in dev mode
against its own DuckDB file, waits for `/health`, curls every endpoint the
frontend depends on, asserts `200` + a non-empty body, prints a pass/fail
table, and stops the server.

**The app must be launched from the repo root** (`/home/Ikey/NeuroNews`) so the
`src.api.app:app` import resolves. The driver `cd`s there itself; if you launch
manually, do it from the repo root. Paths below are relative to the repo root.

## Prerequisites

- **Python 3** with the project deps (`fastapi`, `uvicorn`, `duckdb`).
  Already installed on this box. Verify, and install from `requirements.txt`
  only if this fails:

```bash
python3 -c "import fastapi, uvicorn, duckdb; print('deps OK')"
```

## Run (agent path) — the smoke driver

From anywhere (the script finds the repo root):

```bash
src/api/.claude/skills/run-api/smoke.sh
```

It picks port `8012`, launches its own dev-mode server on a throwaway DuckDB
file, probes everything, and tears down. Expected output — `PASS=7 FAIL=0`,
exit `0`:

```
Launching API on :8012 (dev mode, DB=/tmp/neuronews-smoke-XXXX.duckdb)

CODE   BYTES        ENDPOINT
------------------------------------------------------------
200    352          ✓ /health
200    17055        ✓ /api/v1/news/articles?limit=3
200    1098         ✓ /topics/trending
200    2508         ✓ /api/v1/events/clusters?limit=3
200    3715         ✓ /api/v1/breaking_news
200    2119         ✓ /news_sentiment/topics
200    1012         ✓ /docs

PASS=7 FAIL=0
All endpoints healthy.
```

Pick a different port if `8012` is taken:

```bash
PORT=8013 src/api/.claude/skills/run-api/smoke.sh
```

Probe an **already-running** API instead of launching one (note: the running
instance must be in dev mode, or the WAF will 403 the data endpoints — see
Gotchas):

```bash
API_URL=http://localhost:8012 src/api/.claude/skills/run-api/smoke.sh --no-server
```

## Run (human path) — long-running server

Launch a real server you can curl by hand or point the web frontend at. **Run
from the repo root**, in dev mode, on its own DB file if another instance is up:

```bash
cd /home/Ikey/NeuroNews
NEURONEWS_DEV_MODE=true NEURONEWS_DB_PATH=/tmp/neuronews-dev.duckdb \
  uvicorn src.api.app:app --port 8012
```

Then, in another shell:

```bash
curl -s http://localhost:8012/health
curl -s "http://localhost:8012/api/v1/news/articles?limit=3"
```

Interactive API docs: open `http://localhost:8012/docs`.

## Gotchas

- **The WAF blocks every data endpoint unless `NEURONEWS_DEV_MODE=true`.**
  Without it, `/health` and `/docs` return `200` but `/api/v1/news/articles`
  (and friends) return `403` with
  `{"threat_type":"bot_traffic","code":"WAF_BLOCKED"}` — curl looks like a bot
  to it, and the anonymous rate limit is 10 req/min. Dev mode disables the WAF,
  rate limiting, API-key and RBAC middlewares. The driver always sets it.
- **DuckDB is single-writer — a second instance on the default DB crashes.**
  If an API is already running (e.g. the dev server on :8000 holding
  `data/neuronews.duckdb`), launching another against the same file dies at
  startup with
  `IO Error: Could not set lock on file ".../neuronews.duckdb": Conflicting
  lock is held in ... (PID …)`. Point the second instance at its own file with
  `NEURONEWS_DB_PATH=/tmp/whatever.duckdb`. The driver does this automatically.
- **`/health` is green even when data endpoints are blocked.** `/health` and
  `/docs` don't touch the warehouse or the WAF data path, so they answer `200`
  while `/api/v1/...` 403s (no dev mode) or 500s (DB lock). Don't treat a
  healthy `/health` as "the API is serving data" — probe a real endpoint.
- **Two FastAPI apps exist; this is the real one.** `src/api/app.py`
  (`create_app()`, the WAF/RBAC/DuckDB backend the frontend uses) vs.
  `legacy/services/api/main.py` (a smaller `/ask` Q&A service). For the web dashboard,
  it's `src.api.app:app`.
- **A benign startup error in the log is expected.** Dev mode still logs
  `Error checking table: ... (403) ... DescribeTable ...` from the RBAC system
  probing a (nonexistent) DynamoDB table; the app continues and `Application
  startup complete` follows. Not a failure.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `403 ... "code":"WAF_BLOCKED"` on data endpoints | You launched without dev mode. Set `NEURONEWS_DEV_MODE=true` (the driver does). |
| `500` + log shows `Could not set lock on file ".../neuronews.duckdb"` | Another instance holds the DuckDB lock. Launch with `NEURONEWS_DB_PATH=/tmp/other.duckdb`, or stop the other instance. |
| `Server died on startup` from the driver | Check the printed log tail. Usually the DuckDB lock (above) or a missing dep (`python3 -c "import fastapi, uvicorn, duckdb"`). |
| `Address already in use` | Port taken. Re-run with `PORT=<free port>`. |
| `ModuleNotFoundError: src` | You launched outside the repo root. `cd /home/Ikey/NeuroNews` first (the driver handles this). |
