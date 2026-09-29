# Courts and justice evidence

Evidence for the Legal pack's `courts` and `justice-statistics` features (#2218).

* `source-audit.md` - CJ01 source contracts, licences, rate limits, revision
  models, the party-data minimisation decision and bounded coverage.
* Offline evidence: `tests/unit/domains/test_courts_justice_sources.py`,
  `tests/unit/domains/test_courts_justice_acceptance.py` and the other
  `test_courts_justice_*` suites, over authored fixtures in
  `tests/fixtures/courts_justice/` (a fictional D.D.C. docket filed in 2099,
  fictional parties, a placeholder state `EX`, a fictional police.uk
  neighbourhood and Eurostat figures for 2096-2098).
* Live evidence: none yet. Every provider is `unverified-live`; the dated live
  run and the cited demo belong to CJ14 (#2434) and are recorded here,
  separately from the offline evidence, when they exist.
