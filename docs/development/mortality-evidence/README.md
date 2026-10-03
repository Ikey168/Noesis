# Mortality and health outcomes evidence (clinical.mortality)

Evidence for the Clinical Evidence bundle's proposed `clinical.mortality`
provider, subdomain `mortality-health-outcomes` (wave 2 tracker #2736).

- [source-audit.md](source-audit.md) - CD01: per-source contract, licence,
  access, rate limits, revision model (release vintages, ICD revision and
  coding breaks), the data-minimisation decision and the bounded first
  coverage. WHO Mortality Database, WHO GHO, Eurostat causes of death and the
  UN WPP files are `unverified-live`; the UN Data Portal API is
  `gated-not-granted`; IHME GBD is `not-implemented`. Written without network
  access; terms not re-verified live.
- Offline evidence: none yet. Authored fixtures (reference years 2094-2097,
  releases 2098-2099) arrive with the track's acquisition issues, together
  with `src/ingestion/mortality_sources.py`.
- Live evidence: none yet. A dated live run belongs to the track's "Validate
  live coverage" issue; until it exists every source is `unverified-live` and
  offline coverage is never reported as live coverage.
