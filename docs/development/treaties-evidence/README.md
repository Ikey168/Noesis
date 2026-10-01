# Treaties evidence

Evidence for the Legal pack's treaties provider (`legal.treaties`; features
`treaties-untc`, `treaties-eu`, `treaties-coe`; #2581).

* `source-audit.md` - TR01 source contracts, licence decisions (the UN Treaty
  Collection is declined pending written permission), rate limits, revision
  models, the minimisation decision and bounded coverage, with the official
  URLs and the date each was read.
* Offline evidence: `tests/unit/domains/test_treaties_sources.py`,
  `tests/unit/domains/test_treaties_acceptance.py` and the other
  `test_treaties_*` suites, over authored fixtures in
  `tests/fixtures/treaties/` (a fictional UNTC convention XXIX-99, a fictional
  CETS No. 990 and a fictional EU agreement 22099A0101(01), 2098-2100). The UNTC
  parser runs offline under a test-only accepted licence decision.
* Live evidence: none yet. CELLAR and the Council of Europe are
  `unverified-live` and UNTC is `declined`; the dated live runs and the cited
  demo belong to TR13 (#2645) and are recorded here, separately from the offline
  evidence, when they exist.
