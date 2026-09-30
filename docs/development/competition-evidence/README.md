# Competition cases and state aid evidence

Evidence for the Corporate Ownership pack's optional `competition` feature
(#2217): competition-authority cases (merger control, antitrust, market
investigations), their stages and decision documents, and state-aid awards.

* `source-audit.md` - CS01 (#2312) source contracts, reuse terms, rate limits,
  instruments in and out of scope, stable identifiers, bounded coverage and
  `LIVE_VERIFICATION` per provider.
* Offline evidence: `tests/unit/domains/test_competition_*.py` over authored
  fixtures in `tests/fixtures/competition/` and the pinned source-pack
  fixtures `tests/fixtures/source_packs/ownership-competition-*.json`. Every
  case number, award, party and figure there is fictional (the `Exampla` and
  `Northwind` groups of the ownership fixtures; case numbers `M.99001`,
  `SA.99002`, CMA slug `exampla-northwind-merger-inquiry`, FTC matter
  `2510001`, DOJ case `us-v-exampla-holdings-and-northwind-widgets`).
* Live evidence: none yet. Every provider is `unverified-live`; the dated live
  run and the cited demo belong to CS14 (#2368) and are recorded here,
  separately from the offline evidence, when they exist.
