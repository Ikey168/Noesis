# Internet infrastructure evidence (technology.internet-infrastructure)

Evidence for the Technology bundle's `technology.internet-infrastructure`
provider (wave 2 tracker #2736, subdomain `internet-infrastructure`).

- [source-audit.md](source-audit.md) - II01: per-source contract, licence,
  access, rate limits, revision model, the data-minimisation decision and the
  bounded first coverage. Written without network access; terms not
  re-verified live. RIPEstat, PeeringDB, RDAP, crt.sh and the CT log list are
  `unverified-live`; direct RFC 6962 log access and CAIDA datasets are
  `not-implemented`.
- Machine-readable copy: none yet. It will be added in
  `src/ingestion/internet_infrastructure_sources.py` by the track's
  acquisition issues and must match the audit.
- Offline evidence: none yet. Fixtures will be authored with documentation
  ASNs and prefixes, `example.org` and dates in 2094-2099.
- Live evidence: none. Until a dated live run exists every source is
  `unverified-live`, and offline coverage is never reported as live coverage.
