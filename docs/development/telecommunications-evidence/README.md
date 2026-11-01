# Telecommunications and spectrum evidence (technology.telecommunications)

Evidence for the Technology bundle's proposed `technology.telecommunications`
provider (subdomain `telecommunications-spectrum`, issue #2819, wave 3 tracker
#2814; no per-track tracker yet).

- [source-audit.md](source-audit.md) - TC01: per-source contract, licence,
  access, rate limits, revision model, the data-minimisation decision
  (company licensees only; amateur radio, GMRS and other individual licences
  excluded; no location-level broadband data) and the bounded first coverage.
  Written without network access (publisher hosts blocked, 2026-11-01); terms
  not re-verified live. FCC ULS licences and FCC BDC published summaries
  (conditional on account-free download) are `unverified-live`; the BDC API
  is `gated-not-granted`; BDC location data and the Fabric, the
  Bundesnetzagentur and ITU DataHub are `not-implemented`.
- Offline evidence: none yet. No fixtures, provider or
  `src/ingestion/telecommunications_sources.py` exist.
- Live evidence: none. Until a dated live run exists every kept source is
  `unverified-live`, and `telecommunications-spectrum` stays a gap until a
  fixture-tested provider is classified in `packs/taxonomy.json`.
