# noesis-cli-v1 — stable local operator interface

The `noesis` command is a thin adapter over the public ingestion, KB, Claim
Watch, and Evidence Bundle implementations. It does not maintain alternate
business logic.

## Installation and configuration

Python 3.11 and newer are supported. The base install is the minimal local
stack; extras are `minimal`, `server`, `models`, `vector`, `media`,
`orchestration`, `cloud`, and `full`.

`noesis init` creates `config.json`, `domains.yml`, a mode-0600 DuckDB file,
and a cursor directory. Existing config and domain files are never overwritten.
Resolution order is `--config`, `NOESIS_CONFIG`, the deprecated
`NEURONEWS_CONFIG` alias, then `.noesis/config.json`.

## Commands

- `init`, `doctor`;
- `ingest PATH|URL --domain NAME`;
- `ask QUESTION --domain NAME --format markdown|json`;
- `brief --domains A,B --since TIME --budget N`;
- `watch create|list|poll|pause|resume|delete|scan|replay` and `watches`;
- `export answer|claim|integrity` and `verify BUNDLE`;
- `sync [--daemon] [--interval SECONDS] [--max-items N] [--dry-run]`;
- `serve --surface mcp|api|kb-mcp` (`mcp` is the default).

Every JSON-producing command except the compatibility-preserved Evidence
Bundle verifier uses this envelope:

```json
{
  "cli_contract": "noesis-cli-v1",
  "command": "ask",
  "ok": true,
  "data": {}
}
```

Canonical contract results are nested unchanged under `data`. Errors replace
`data` with `{code, message, repair?}`. JSON stdout never contains progress
text. The verifier retains its established `VerificationResult` JSON exactly.
For `sync --daemon --json`, stdout is a JSON Lines stream: one complete
`noesis-cli-v1` envelope per sync pass followed by a final `sync.daemon`
envelope on graceful shutdown.

Exit codes are stable: `0` success, `1` failed verification/diagnostic,
`2` incomplete bundle or argument parsing, `3` configuration, `4` missing
optional runtime dependency, `5` operation failure, and `6` missing destructive
confirmation.

## Privacy and resumability

Private evidence is excluded from exports unless `--include-private` is
explicit. `watch poll` reads and atomically advances a per-watch opaque cursor;
an explicit `--cursor` takes precedence and `--no-save-cursor` disables the
write. Watch deletion requires either typing the complete watch id in an
interactive terminal or passing `--yes`, including noninteractive use.

Ingestion uses the unified `DocumentStore` and membership pass. Document ids
and content hashes make interrupted retries idempotent. URL fetching occurs
only when the operator explicitly supplies an HTTP(S) URL.

`noesis sync` performs one bounded synchronization pass. It fetches only
explicitly enabled inbox/feed subscriptions, promotes new or changed retained
feed revisions through the same production `ingest → extract → resolve →
index` path, refreshes domain membership, commits a consolidation watermark,
runs the current principal's watches, and only recovers/inspects maintenance
state. It does not start Deep Research, execute queued source-pack maintenance
jobs, or initiate paid acquisition. Durable run receipts and per-feed revision
checkpoints make restart retries idempotent. `--dry-run` performs no network
fetches and creates no sync tables. `sync --daemon` holds a single-instance
advisory lock, handles SIGINT/SIGTERM gracefully, and uses bounded exponential
backoff between degraded passes.

Doctor performs no network access and never prints credential values.

## Compatibility policy

Existing Python, MCP, REST, and `python -m src.evidence_bundle` entry points
remain supported. `NEURONEWS_*` configuration aliases are deprecated but will
remain accepted throughout the 1.x series. Their eventual removal requires a
2.0 contract, release-note notice, and at least one minor release of warnings.
