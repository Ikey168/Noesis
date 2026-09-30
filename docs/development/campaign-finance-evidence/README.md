# Campaign finance evidence

Evidence for the Political pack's campaign-finance features (#2209).

* `source-audit.md` - CF01 source contracts (OpenFEC API, FEC bulk data, UK
  Electoral Commission donations and spending search), licences and the FEC
  reuse restriction on contributor names, rate limits, amendment models, the
  **donor data-minimisation decision** and the bounded first coverage.
* Offline evidence: `tests/unit/domains/test_campaign_finance_sources.py`,
  `tests/unit/domains/test_campaign_finance_acceptance.py` and the other
  `test_campaign_finance_*` suites, over authored fixtures in
  `tests/fixtures/campaign_finance/` (fictional committees, candidates,
  organisations and placeholder individual donors that the parser discards).
* Live evidence: none yet. Every source is `unverified-live`; the dated live
  run and cited demo belong to CF14 (#2529) and are recorded here, separately
  from the offline evidence, when they exist.
