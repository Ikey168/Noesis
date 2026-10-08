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
| `eia-bulk` | manifest `last_updated` | `datasets` + `series` / `series_prefix` / `all_series`, `since_period` |
| `ember-generation` | file Last-Modified | `frequency`, `iso3` / `areas` / `all_areas`, `sources`, `since` (CC BY 4.0) |
| `fas-psd` | file Last-Modified | `commodity_codes` / `commodities` / `all_commodities`, `countries`, `attributes`, `since_market_year` |
| `opensanctions-targets` | artifact version (SHA-1 verified) | `collection`, `ids`, `names`, `countries`, `datasets`, `schemata`, or `all_targets` (CC BY-NC 4.0; phones/emails dropped) |
| `sam-opportunities-extract` | retrieval day | `naics_prefixes`, `agencies`, `keywords`, `set_aside`, `posted_since`, `active_only` (contacts and description dropped) |
| `iati-activities` | Bulk Data Service index time (per-file SHA-1) | `publishers` (required), `recipient_countries`, `statuses`, `identifiers` |
| `courtlistener-bulk` | newest non-empty file date | `tables` (courts, `people-db-*`, `financial-disclosures*`), `where` |
| `openalex-snapshot` (documents) | snapshot date (manifest) | `dois` / `ids` / `primary_topic_ids` / `source_ids` / `title_contains`, `publication_year_from`/`_to`, `types`, `languages`, `updated_since` |
| `pubmed-baseline` (documents) | baseline year + last update file | `pmids` / `mesh` / `journals` / `keywords`, `year_from`/`_to`, `include_baseline`, `include_updates` |
| `s2-datasets` | S2AG release (or diff range) | `dataset` (abstracts/papers), `corpus_ids` / `dois` / `all_records`, `release`, `since_release` — needs `SEMANTIC_SCHOLAR_API_KEY` |
| `openaq-archive` | listing date | `location_ids` (required), `date_from`, `date_to` (≤ 24 months), `parameters` |

### Large dumps: what to expect

- **OpenAlex snapshot** (2026-09-23: 476 million works, 2,040 Parquet files,
  ~707 GB) is never downloaded: DuckDB queries each part on S3 and transfers only
  the needed columns. A title search took about 100 s per ~880 MB part on the
  server (a full pass ≈ 2–3 days), so run it under `--max-files`/`--max-seconds`
  over several nights and use `updated_since` for later runs. Works map to the
  same `document_id` as the `openalex` connector. Report a snapshot extract as its
  own source (filter + snapshot date); it is not the API's relevance search.
- **PubMed** baseline (1,334 files, ~20–40 MB each) plus daily update files; each
  file's published MD5 is fetched and verified before parsing; `DeleteCitation`
  PMIDs go to a `deletions` table. About 17 s per file on the server.
- **Semantic Scholar Datasets** need the API key for shard links (pre-signed S3
  URLs on `ai2-s2ag.s3.amazonaws.com`; the key is never sent to S3). Not yet
  verified live — no key configured when written.
- **ORCID public data file** — not implemented: the 2024 file is about 730 GB
  (summaries) plus 3.1 TB (activities) uncompressed, and the free Public API
  covers record lookups.

Remote (`remote` mode) queries do not count bytes; their budgets are files and
seconds.

Not available as bulk data (checked October 2026):

- **CourtListener case law** — `dockets`, `opinions`, `opinion-clusters` and
  `citations` have been published as empty files since 2024-03-01; use the API
  token within its quota or a free EDU membership.
- **Kystverket open AIS** — `ais-public.kystverket.no` did not answer from the
  server or a desktop, and the historical service (`hais.kystverket.no`) is an
  interactive order flow, so there is no automatable keyless bulk route yet.

Redirects are followed only when every hop stays on the adapter's allowlist and
resolves to a public address (SAM.gov's extract redirects to a pre-signed S3
URL on `falextracts.s3.amazonaws.com`, which is allowlisted).

## Scheduled jobs on the server

```
/srv/bulk/jobs/<job>.json   adapter, params, sink, namespace, budgets
/srv/bulk/state/            manifests, markers, subsets, receipts  (backed up)
/srv/bulk/tmp/              scratch for downloads                  (not backed up)
```

`deploy/bulk/noesis-bulk@.service` and `.timer` run a job weekly with low CPU/IO
priority and a 4 GB memory cap; keys come from OpenBao through `bao-env` (exit
75 = sealed). Exit code 2 counts as success because the next run resumes.
