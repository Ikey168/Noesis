# Regulatory enforcement evidence

Evidence for the Legal pack's optional regulatory enforcement features
(#2651): `enforcement-sec`, `enforcement-fca`, `enforcement-epa` and
`enforcement-edpb` (provider `legal.enforcement`, subdomain
`regulatory-enforcement`).

* `source-audit.md` - EN01 (#2655) source contracts, key handling, reuse terms,
  rate limits, revision and removal models, the data-minimisation decision for
  named individuals, bounded coverage and `LIVE_VERIFICATION` per provider.
  Written without network access to the publishers; terms were not re-verified
  live.
* Offline evidence: `tests/unit/domains/test_enforcement_*.py` over authored
  fixtures in `tests/fixtures/enforcement/` and the pinned source-pack fixtures
  `tests/fixtures/source_packs/legal-enforcement-*.json`. Every action,
  respondent, identifier, facility and figure there is fictional (the `Exampla`
  and `Northwind` groups of the ownership fixtures; SEC `LR-99901` and
  `34-99902`, FCA notices `exampla-uk-limited-2025` and
  `northwind-brokers-limited-2024`, ECHO cases `09-2025-9901` and
  `05-2024-9902`, EDPB entries `exampla-intermediate-bv-security-of-processing`
  and `anonymised-controller-reprimand-2025`). The individuals named in the
  fixtures are placeholders the adapter never stores.
* Live evidence: none. Every provider is `unverified-live`; the dated live run
  and the cited demo belong to EN14 (#2720) and are recorded here, separately
  from the offline evidence, when they exist.
