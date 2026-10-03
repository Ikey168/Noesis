# Cyber incidents evidence (technology.cyber-incidents)

Evidence for the Technology bundle's `technology.cyber-incidents` provider
(wave 2 tracker #2736, subdomain `cyber-incidents`).

- [source-audit.md](source-audit.md) - CY01: per-source contract, licence,
  access, rate limits, revision model (including 8-K/A amendments), the
  data-minimisation decision and the bounded first coverage. Written without
  network access; terms not re-verified live. SEC 8-K Item 1.05 filings are
  `unverified-live`; the Washington Attorney General list is `unverified-live`
  on condition that its structured copy exists; the HHS OCR breach portal is
  `not-implemented`.
- Machine-readable copy: none yet. It will be added in
  `src/ingestion/cyber_incidents_sources.py` by the track's acquisition issues
  and must match the audit.
- Offline evidence: none yet. Fixtures will be authored for fictional issuers
  and organisations with dates in 2094-2099.
- Live evidence: none. Until a dated live run exists every source is
  `unverified-live`, and offline coverage is never reported as live coverage.
