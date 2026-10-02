# Fact-checks evidence

Evidence for the News pack's fact-checks provider `news.fact-checks` (#2659).

* `source-audit.md` - FC01 source contracts (Google Fact Check Tools API, Data
  Commons ClaimReview feed, IFCN signatory list), key handling, licences and
  terms (not re-verified live), rate limits, revision and removal models, the
  **data-minimisation decision** and the bounded first coverage.
* Offline evidence: `tests/unit/domains/test_fact_checks_sources.py`,
  `tests/unit/domains/test_fact_checks_acceptance.py` and the other
  `test_fact_checks_*` suites, over authored fixtures in
  `tests/fixtures/fact_checks/` (fictional publishers, claimants and claims;
  personal fields present on purpose so the tests show they are dropped).
* Live evidence: none yet. Every source is `unverified-live`; the dated live
  run and cited demo belong to FC13 (#2722) and are recorded here, separately
  from the offline evidence, when they exist.
