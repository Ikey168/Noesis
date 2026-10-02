# Treaties evidence

Evidence for the Legal pack's `legal.treaties` provider (#2581) and its
optional `treaties-untc`, `treaties-eu` and `treaties-coe` features.

* `source-audit.md` - TR01 source contracts, licences, rate limits, revision
  models, the minimisation decision and bounded coverage. Written offline; the
  sources' terms were not re-verified live.
* Offline evidence: `tests/unit/domains/test_treaties_sources.py`,
  `tests/unit/domains/test_treaties_acceptance.py` and the other
  `test_treaties_*` suites, over authored fixtures in `tests/fixtures/treaties/`
  (a fictional UNTC treaty `XXVII-99`, a fictional EU agreement
  `22090A0510(01)`, a fictional Council of Europe treaty `CETS 999`, fictional
  states Exampland, Northwind Republic, Southland and Oldland with the
  user-assigned codes `XEA`/`XNW`, years 2090-2099).
* Live evidence: none yet. Every provider is `unverified-live`; the dated live
  run and the cited demo belong to TR13 (#2645) and are recorded here,
  separately from the offline evidence, when they exist.
