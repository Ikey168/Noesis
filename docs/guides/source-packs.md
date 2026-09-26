# Production source packs

Source packs turn Noesis connectors into versioned operational configuration.
Each `noesis-source-pack-v1` manifest pins source identity, publisher and scope,
licensing, connector and mapping versions, temporal semantics, schedules,
budgets, secret references, health policy, and an offline fixture.

Six built-in packs cover the primary Noesis workloads:

- `research-discovery`: Crossref and OpenAlex discovery and citation metadata;
- `official-political-records`: Federal Register, EUR-Lex, Elections Canada,
  and Bundestag DIP official records;
- `economic-statistics-and-filings`: World Bank, Eurostat, FRED, and SEC EDGAR;
- `bounded-public-osint`: news discovery, archive indices, RDAP, and GDELT under
  the existing OSINT review policy;
- `technical-software-knowledge`: Git, PyPI, npm, OSV, and RFC records;
- `primary-scientific-evidence`: Europe PMC, arXiv, and DataCite primary works,
  versions, corrections, and datasets.

Run deterministic validation and fixture replay without network access:

```bash
make source-pack-check
```

Live probes are never part of default CI. Operators may run a small, bounded
reachability check explicitly:

```bash
python scripts/source_pack_conformance.py --live --max-requests 5
```

Credentials never appear in a manifest. A source declares a `NOESIS_*`
`secret_ref`; the runtime resolves it through operator configuration. Required
secrets that are absent make that source unready while the pack reports honest
partial coverage. Live failures are classified as configuration,
authentication, provider drift, rate limiting, transient availability, or a
Noesis regression. Reports contain metadata and receipts, never response bodies
or credential values.

Installed versions are immutable. Installing the same bytes is idempotent;
upgrades retain previous versions, and enable/disable changes only the selected
pack. Fixture hashes and expected normalized-output hashes make provider mapping
drift visible in offline CI.

Before upgrading, call the read-only MCP tool `preview_source_pack_upgrade` with
the complete candidate manifest. For example, install
`config/source_packs/research.json`, change its version to `1.3.0`, and change
the `crossref-works` mapping version in the candidate. The response pins the
installed and candidate hashes and lists source additions, removals, and field
changes, including endpoint, connector, mapping, domain, credential declaration,
and license terms. Reordering sources or operations does not produce a change.
The preview does not check provider availability or apply the upgrade. A changed
manifest with an already installed version and a version downgrade are rejected.
The tool requires `knowledge:read` or `operator`; it returns credential reference
names only, never resolved secret values.

## Technical dependency inventories

`import_technical_inventory` accepts the contents of a `package-lock.json`
(lockfile versions 1–3) or a pinned `requirements.txt`, plus `format` set to
that filename. For example, call it with
`{"content":"Foo_Bar==1.2.3\n", "format":"requirements.txt"}`. It returns
an owner-scoped inventory ID, an exact input SHA-256 hash, normalized package
coordinates, versions, direct/transitive origins, and explicit `unresolved` or
`unsupported` labels. `inspect_technical_inventory` accepts that ID and bounded
`limit`/`offset` to read entries and IDs of already acquired package and OSV
records in the public Technology graph. A matching advisory link is evidence
of identity only; version impact is assessed separately. Input is limited to
2 MB and 5,000 entries, and imports never run project code or fetch packages.
Imports require `knowledge:technical:write`; reads require
`knowledge:technical:read`. Fixture tests cover parsing and owner isolation;
live provider availability and vulnerability impact are not inferred from an
inventory import.

## Execution runtime

Installed packs can be executed incrementally or as bounded backfills. Before
the first run, an operator records acceptance of each source's current terms
with `accept_source_pack_license`. Redistribution requires a separate explicit
acceptance receipt. `preflight_source_pack_run` then verifies the immutable
manifest, required credentials, public HTTPS resolution, terms receipt, and
source circuit state without returning secret values.

`run_source_pack_execution` is network-free by default and replays the pinned
fixtures. Set `live_network=true` only for an intended production collection.
The request also records `network=live`, so the live boundary is visible in its
stable request hash. Each execution persists page cursors, counts, output
hashes, quarantine entries, circuit state, and a per-pack committed watermark.
Retries resume from the last durable page; backfills never advance the live
incremental cursor.

For example, accept the `crossref-works` terms, then pass this request to
`preflight_source_pack_run` and `run_source_pack_execution`:

```json
{
  "pack_id": "research-discovery",
  "run_key": "research-daily-2026-09-04",
  "operation": "search",
  "source_ids": ["crossref-works"],
  "required_sources": ["crossref-works"],
  "parameters": {"query": "causal inference"},
  "max_pages": 5,
  "max_results": 100,
  "timeout_ms": 30000
}
```

A historical run sets `mode` to `backfill` and supplies a bounded `backfill`
object such as `{"from_ms": 1756684800000, "to_ms": 1756771200000}`.

Useful MCP controls include:

- `inspect_source_pack_run`, `cancel_source_pack_run`, and
  `replay_source_pack_run`;
- `list_source_pack_quarantine` and `retry_source_pack_quarantine`;
- `set_source_pack_schedule` and `list_source_pack_schedules`;
- `source_pack_runtime_coverage` for six-domain execution coverage.

Schedules contain only bounded intervals and never credentials. Overlapping
runs for one pack are rejected. Optional sources can yield an honest partial
receipt; failure of a required source prevents watermark publication.

Run the complete network-free execution showcase with:

```bash
make source-pack-runtime-check
```

For explicit date, geography, language and topic scope across pinned runs, use
the [investigation coverage guide](investigation-coverage.md). It distinguishes
observed evidence, successful empty searches, unavailable sources, and sources
that were not attempted.
