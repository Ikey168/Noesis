# Oceans and marine environment evidence (environment.marine)

Evidence for the Climate and Environment bundle's proposed `environment.marine`
provider (subdomain `oceans-marine`, wave 2 tracker #2736).

- [source-audit.md](source-audit.md) - OM01: per-source contract, licence,
  access, rate limits, revision model (OISST preliminary and final, Argo
  real-time and delayed mode, Natura 2000 releases), the data-minimisation
  decision and the bounded first coverage. Written without network access;
  terms not re-verified live.
- Offline evidence: none yet. Fixtures will be authored and synthetic (dates
  2094-2099) and live in the tests, never here.
- Live evidence: none yet. ERDDAP OISST, Argo via ERDDAP and Natura 2000 are
  `unverified-live`; Copernicus Marine is `gated-not-granted`; WDPA and the
  Argo GDAC and Argovis paths are `not-implemented`. Offline coverage is never
  reported as live coverage.
