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
- `serve --surface api|kb-mcp`.

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
only when the operator explicitly supplies an HTTP(S) URL. Doctor performs no
network access and never prints credential values.

## Compatibility policy

Existing Python, MCP, REST, and `python -m src.evidence_bundle` entry points
remain supported. `NEURONEWS_*` configuration aliases are deprecated but will
remain accepted throughout the 1.x series. Their eventual removal requires a
2.0 contract, release-note notice, and at least one minor release of warnings.
