# Animal and veterinary health evidence (clinical.animal-health)

Evidence for the Clinical Evidence bundle's proposed `clinical.animal-health`
provider, subdomain `animal-health` (wave 2 tracker #2736).

- [source-audit.md](source-audit.md) - AH01: per-source contract, licence,
  access, rate limits, revision model (record versions, outbreak event updates
  and closures), the data-minimisation decision (published admin level only;
  no coordinates or holding identifiers) and the bounded first coverage. EFSA
  Knowledge Junction records on Zenodo are `unverified-live`; WOAH WAHIS and
  the EFSA dashboards are `not-implemented`; FAO EMPRES-i+ is
  `gated-not-granted`. Written without network access; terms not re-verified
  live.
- Offline evidence: none yet. Authored fixtures (reference periods 2094-2097,
  releases 2098-2099, fictional record ids) arrive with the track's
  acquisition issues, together with `src/ingestion/animal_health_sources.py`.
- Live evidence: none yet. A dated live run belongs to the track's "Validate
  live coverage" issue; until it exists every source is `unverified-live` and
  offline coverage is never reported as live coverage.
