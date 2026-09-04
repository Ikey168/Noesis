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
