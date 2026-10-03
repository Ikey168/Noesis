# Industry and business statistics evidence (economics.business)

Evidence for the Economics bundle's proposed `economics.business` provider
(subdomain `industry-business`, track #2738, wave 2 tracker #2736).

- [source-audit.md](source-audit.md) - IB01: per-source contract, licence,
  access, rate limits, revision model, CBP disclosure-protection flags, the
  data-minimisation decision and the bounded first coverage for Eurostat
  short-term business statistics, Eurostat business demography and US Census
  County Business Patterns (optional key `NOESIS_CENSUS_API_KEY`). Written
  without network access; terms not re-verified live.
- Offline evidence: authored fixtures (fictional reference periods 2094-2097,
  release dates in 2024 so runtime retrievals never precede them; see the
  audit's deviation note): `tests/fixtures/source_packs/economic-business-*.json`
  (pinned in `config/source_packs/economic.json` 1.7.0) and the revision,
  rebase and concordance fixtures in `tests/fixtures/business/`.
- Live evidence: none yet. Until a dated live run exists every source is
  `unverified-live` and offline coverage is never reported as live coverage.
