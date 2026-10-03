# Land, soils and geology evidence (environment.land)

Evidence for the Climate and Environment bundle's proposed `environment.land`
provider (subdomain `land-soils-geology`, wave 2 tracker #2736).

- [source-audit.md](source-audit.md) - LN01: per-source contract, licence,
  access, rate limits, revision model (CORINE editions and change layers, FRA
  assessment rounds, map editions), the data-minimisation decision and the
  bounded first coverage. Written without network access; terms not
  re-verified live.
- Offline evidence: none yet. Fixtures will be authored and synthetic (dates
  2094-2099) and live in the tests, never here.
- Live evidence: none yet. CLC (EEA vector service), CLC5 (BKG), FAO FRA and
  the BGR overview maps are `unverified-live`; the CLMS download API is
  `gated-not-granted`; ESDAC and BGS are `not-implemented`. Offline coverage is
  never reported as live coverage.
