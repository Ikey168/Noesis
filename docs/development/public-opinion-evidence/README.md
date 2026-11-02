# Public opinion and wellbeing evidence (society.public-opinion)

Evidence for the Society bundle's proposed `society.public-opinion` provider
(subdomain `public-opinion-wellbeing`, issue #2817, wave 3 tracker #2814; no
per-track tracker yet).

- [source-audit.md](source-audit.md) - PO01: per-source contract, licence,
  access, rate limits, revision model, the metadata every value carries
  (question wording, fieldwork, sample), the data-minimisation decision and
  the bounded first coverage. Written without network access (publisher hosts
  blocked, 2026-11-01); terms not re-verified live. OECD How's Life (existing
  OECD SDMX path) and the Commission's Eurobarometer topline tables
  (conditional on a tabular file) are `unverified-live`; Eurobarometer
  microdata at GESIS and ESS microdata are `gated-not-granted`; ESS
  aggregates are `not-implemented`.
- Offline evidence: none yet. No fixtures, provider or
  `src/ingestion/public_opinion_sources.py` exist.
- Live evidence: none. Until a dated live run exists every kept source is
  `unverified-live`, and `public-opinion-wellbeing` stays a gap until a
  fixture-tested provider is classified in `packs/taxonomy.json`.
