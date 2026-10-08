# Bulk releases and free-tier quotas

Free alternatives for connectors whose API is paid or capped (see
[Connector API keys](connector-api-keys.md)): download a provider's bulk
release, keep only the filtered subset, and cite the release as a vintage.
Tracker: #2839.

## Free-tier quotas (`src/ingestion/quota.py`)

Every request from the scholarly connectors, the abstract backfill, source-pack
runs and bulk downloads passes a shared ledger keyed by **API host**. Limits are
declared in `config/source_quotas.json` (per second/minute/hour/day/week/month
or a fixed number of seconds, in requests or bytes).

- A request that would exceed a limit waits if the window ends within 5 s,
  otherwise it raises `QuotaDeferred` with the retry time. Source-pack runs turn
  this into their existing `rate_limited` error; connector harvests re-raise it
  so a spent budget is never reported as "zero hits".
- An HTTP 429/503 with `Retry-After` blocks the host until then.
- Usage lives in `NOESIS_QUOTA_DB` (default `~/.local/state/noesis/quota.sqlite`),
  shared by all processes of the same user. `NOESIS_QUOTA_DISABLED=1` turns it
  off (the test suite does).
- Inspect: MCP `inspect_source_quotas(host=None)` or
  `python -m src.ingestion.bulk quotas`.

## Bulk-release runner (`src/ingestion/bulk/`)

```bash
python -m src.ingestion.bulk list
python -m src.ingestion.bulk run bls-flat-files \
    --params '{"survey": "cu", "series": ["CUSR0000SA0"], "since_year": 2015}' --dry-run
python -m src.ingestion.bulk run companies-house-snapshot \
    --params '{"sic_prefix": "62"}' --sink dataset --namespace bulk-companies
python -m src.ingestion.bulk job /srv/bulk/jobs/bls-cpi.json
```

A run processes one release of one adapter:

1. **Manifest first** — provider, release id, files (size, ETag, Last-Modified,
   published checksum) and licence in `state/<adapter>/<release>/manifest.json`.
2. **File by file, resumable** — a completion marker per file; an interrupted
   download resumes from its `.part` file (HTTP Range). A published checksum
   that does not match moves the file to `quarantine/`.
3. **Keep the subset** — the adapter's filter decides what is kept; the subset is
   written to `subset/<table>.jsonl.gz`, and optionally
   - `--sink dataset`: the dataset and release are registered in the dataset
     store and rows ingested in chunks of up to 10,000, partitioned by file;
   - `--sink documents --domain D`: papers go through the gateway ingest.
   Raw files are deleted after processing (`--keep-raw` keeps them).
4. **Incremental** — files whose fingerprint matches a completed file of an
   earlier release are skipped (`--full` reprocesses them).
5. **Budgets** — `--max-bytes`, `--max-files`, `--max-seconds` stop a run cleanly
   (`budget_exhausted`, resumable); a spent quota gives `deferred`.
6. **Receipt** — `receipts/<time>.json` per run with status, files, records,
   bytes, errors and outputs. Exit code 0 = complete, 2 = deferred or budget
   exhausted, 1 = errors.

Adapters: `BulkAdapter` with `list_release()` (no bulk download) and
`process()` (records of one file). Files are `download` (scratch file, e.g.
ZIP), `stream` (read while downloading, byte-capped) or `remote` (queried in
place, e.g. DuckDB over S3). Network rules match the scholarly connectors: HTTPS,
per-adapter host allowlist, public addresses only, contact User-Agent.

| Adapter | Release | Filter (at least one required) |
|---|---|---|
| `bls-flat-files` | newest Last-Modified of the selected files | `survey` + `series` / `series_prefix` / `all_series`, `since_year`, `files` |
| `companies-house-snapshot` | monthly snapshot date | `company_numbers`, `postcode_prefix`, `sic_prefix`, `status`, `incorporated_since`, or `all_companies` |

## Scheduled jobs on the server

```
/srv/bulk/jobs/<job>.json   adapter, params, sink, namespace, budgets
/srv/bulk/state/            manifests, markers, subsets, receipts  (backed up)
/srv/bulk/tmp/              scratch for downloads                  (not backed up)
```

`deploy/bulk/noesis-bulk@.service` and `.timer` run a job weekly with low CPU/IO
priority and a 4 GB memory cap; keys come from OpenBao through `bao-env` (exit
75 = sealed). Exit code 2 counts as success because the next run resumes.
