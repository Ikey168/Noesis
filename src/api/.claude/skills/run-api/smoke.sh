#!/usr/bin/env bash
# Smoke driver for the NeuroNews FastAPI backend (src/api/app.py).
#
# Launches the API in dev mode against its OWN DuckDB file (so it never fights
# an already-running instance for the single-writer lock), waits for /health,
# curls every data endpoint the web frontend depends on, asserts 200 + a
# non-empty JSON body, prints a pass/fail table, and stops the server.
#
# Runs fully standalone: no AWS, no Snowflake, no network. The warehouse is a
# local DuckDB file seeded with sample news on first request.
#
# Usage:
#   src/api/.claude/skills/run-api/smoke.sh                 # full run, own server
#   PORT=8020 src/api/.claude/skills/run-api/smoke.sh       # pick the port
#   API_URL=http://localhost:8000 .../smoke.sh --no-server  # probe a running API
#
# Must be runnable from anywhere; it cd's to the repo root itself so the
# `src.api.app:app` import resolves.
set -uo pipefail

# --- locate repo root (four levels up from this script) --------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../../.." && pwd)"   # 5 up: run-api -> skills -> .claude -> api -> src -> repo
cd "$REPO_ROOT"

PORT="${PORT:-8012}"
API_URL="${API_URL:-http://localhost:$PORT}"
START_SERVER=1
[ "${1:-}" = "--no-server" ] && START_SERVER=0

SERVER_PID=""
cleanup() { [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null; }
trap cleanup EXIT

if [ "$START_SERVER" = "1" ]; then
  # Own DB file => no lock conflict with another running instance.
  DB_PATH="${NEURONEWS_DB_PATH:-$(mktemp -u /tmp/neuronews-smoke-XXXX.duckdb)}"
  LOG="$(mktemp /tmp/neuronews-api-XXXX.log)"
  echo "Launching API on :$PORT (dev mode, DB=$DB_PATH)"
  NEURONEWS_DEV_MODE=true NEURONEWS_DB_PATH="$DB_PATH" \
    uvicorn src.api.app:app --port "$PORT" >"$LOG" 2>&1 &
  SERVER_PID=$!

  # Wait up to 30s for /health.
  for _ in $(seq 1 30); do
    if curl -sf -o /dev/null "$API_URL/health" 2>/dev/null; then break; fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "Server died on startup. Log tail:"; tail -20 "$LOG"; exit 1
    fi
    sleep 1
  done
fi

# --- endpoints the web frontend (apps/web) actually calls ------------------
declare -a ENDPOINTS=(
  "/health"
  "/api/v1/news/articles?limit=3"
  "/topics/trending"
  "/api/v1/events/clusters?limit=3"
  "/api/v1/breaking_news"
  "/news_sentiment/topics"
  "/api/v1/ui/context"
  "/api/v1/ui/panels"
  "/docs"
)

pass=0; fail=0
printf "\n%-6s %-12s %s\n" "CODE" "BYTES" "ENDPOINT"
printf "%s\n" "------------------------------------------------------------"
for ep in "${ENDPOINTS[@]}"; do
  body="$(curl -s "$API_URL$ep")"
  code="$(curl -s -o /dev/null -w "%{http_code}" "$API_URL$ep")"
  bytes=${#body}
  ok="✗"
  # 200 and a non-trivial body (the JSON error envelope is short; real data isn't)
  if [ "$code" = "200" ] && [ "$bytes" -gt 30 ]; then ok="✓"; pass=$((pass+1)); else fail=$((fail+1)); fi
  printf "%-6s %-12s %s %s\n" "$code" "$bytes" "$ok" "$ep"
done

echo
echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ] && echo "All endpoints healthy." || echo "Some endpoints failed (see above)."
exit "$fail"
