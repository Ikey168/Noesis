# Local-first CLI

Install the small local stack and initialize a private workspace:

```console
python -m pip install -e ".[minimal]"
noesis init --non-interactive
noesis doctor
```

Ingest and ask without Docker, cloud credentials, or an API key:

```console
noesis ingest examples/quickstart/moon-mission.md --domain local
noesis ask "What was the mission result?" --domain local
```

Ingestion automatically runs the bounded production path through document
revisioning, claim extraction, resolution, and indexing. If
`TYPESAFE_API_KEY` is set, Jev is the primary claim classifier; low-confidence
or failed Jev calls fall back to the already-cached pinned local classifier.
Without a TypeSafe key the path stays fully local. If neither classifier is
available, the document is still indexed, the ingest result reports degraded
coverage, and answers can fall back to cited source passages.

The same Jev-first policy applies to stance and frame classification. Tune the
fallback gates with `NOESIS_JEV_NOUL_MARGIN`,
`NOESIS_JEV_CHOICE_CONFIDENCE`, and `NOESIS_JEV_FRAME_MARGIN`, or force the
strictly local classifiers with `NOESIS_JEV_ENABLED=false`.

Machine consumers use `--format json` for answers and briefs, or `--json` for
lifecycle commands. JSON stdout is reserved for the documented contract;
diagnostic/progress text goes to stderr.

Exporting a private local answer is deliberately explicit:

```console
noesis export answer \
  --domain local \
  --question "What was the mission result?" \
  --include-private \
  --output answer.bundle.json
noesis verify answer.bundle.json
```

Claim Watch polling persists its opaque cursor by default:

```console
noesis watch create --domain local --type topic --value mission --json
noesis watch poll WATCH_ID --json
noesis watches --domain local --json
noesis watch pause WATCH_ID
noesis watch resume WATCH_ID
noesis watch delete WATCH_ID --yes
```

Use `--cursor-file PATH` to control cursor storage or `--no-save-cursor` for a
read without persistence. An explicit `--cursor` overrides the saved value.

Keep configured feed/inbox subscriptions current with the persistent sync loop:

```console
noesis sync
noesis sync --dry-run --json
noesis sync --daemon
noesis sync --daemon --interval 60 --max-items 200
```

A normal `sync` is one bounded pass. Daemon mode repeats the same pass, holds a
single-instance lock at `.noesis/sync.lock`, persists run receipts and per-feed
revision checkpoints in DuckDB, handles SIGINT/SIGTERM gracefully, and uses
bounded exponential backoff after partial/failed passes. Only explicitly
enabled subscriptions are fetched. New revisions enter the same production
`ingest → extract → resolve → index` workflow as `noesis ingest`; then domain
membership and watches are advanced. Maintenance is inspect/recovery-only:
queued source-pack jobs and Deep Research are not executed, and paid acquisition
is never started implicitly. In daemon mode `--json` emits JSON Lines, one
`noesis-cli-v1` envelope per pass plus a final shutdown envelope.

The supported server launchers validate the local config and report the bind
address, auth posture, endpoint, and enabled surface before starting. The
default is the curated Noesis MCP gateway:

```console
python -m pip install -e ".[server]"
noesis serve
# default: http://127.0.0.1:8100/mcp

# explicit advanced/compatibility surfaces:
noesis serve --surface api
noesis serve --surface kb-mcp --transport http --port 8100
```

`noesis serve --dry-run --json` validates and reports configuration without
binding a socket. See [the CLI contract](../../contracts/noesis-cli-v1.md) for
output shapes, exit codes, privacy defaults, and compatibility policy.
