# Tax transparency evidence (economics.tax)

Evidence for the Economics bundle's `economics.tax` provider (subdomain
`public-finance`, issue #2889).

- [source-audit.md](source-audit.md) - TX01: per-source contract, licence,
  access, rate limits, revision (edition) model, the data-minimisation decision
  and the bounded first coverage for the OECD Corporate Tax Statistics CbCR
  aggregates and corporate tax rates and the EU Tax Observatory offshore wealth
  and billionaire effective tax rate estimates; Tax Justice Network indices
  recorded as `not-implemented` (registration-gated, non-commercial-only free
  licence); public CbCR under Directive (EU) 2021/2101 as link-only
  `reference-only` records. Written without live access to the publishers;
  terms not re-verified live.
- Offline evidence: authored fixtures (fictional reference years 2091-2093,
  edition release dates in 2024 and 2025):
  `tests/fixtures/source_packs/economic-tax-transparency-*.json` (pinned in
  `config/source_packs/economic-tax-transparency.json` 1.0.0), the second-edition fixtures
  and the public CbCR reference fixture in `tests/fixtures/tax/`.
- Offline acceptance: `tests/unit/domains/test_tax_acceptance.py`
  (jurisdiction to cited CbCR aggregates, rates and estimates as of an edition,
  company to public CbCR references through reviewed identity, sockets blocked)
  and the [guide](../../guides/economics-tax.md). Status: covered offline only.
- Live evidence: none yet. Until a dated live run exists every acquired source
  is `unverified-live` and offline coverage is never reported as live coverage.
