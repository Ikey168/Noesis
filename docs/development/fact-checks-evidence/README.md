# Fact-checks evidence

Evidence for the News bundle's `news.fact-checks` provider (#2659).

* `source-audit.md` - FC01 source contracts (Google Fact Check Tools API, Data
  Commons ClaimReview feed and research dataset, IFCN signatories listing),
  access and key handling, licences, rate limits, revision and removal models,
  the **claimant data-minimisation decision**, the bounded first coverage and
  the source-pack location.
* Offline evidence: `tests/unit/domains/test_fact_checks_sources.py`,
  `tests/unit/domains/test_fact_checks_acceptance.py` and the other
  `test_fact_checks_*` suites, over authored fixtures in
  `tests/fixtures/fact_checks/` (fictional publishers, claimants and claims;
  placeholder reviewer names, images and job titles that the parsers discard).
* Live evidence: none yet. Every source is `unverified-live`; the IFCN listing
  additionally refuses a live fetch until an operator records a terms
  confirmation. The dated live runs and the cited demo belong to FC13 (#2722)
  and are recorded here, separately from the offline evidence, when they exist.
