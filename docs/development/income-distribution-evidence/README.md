# Income, poverty and inequality evidence (society.income)

Evidence for the Society bundle's `society.income` provider (#2583).

- [source-audit.md](source-audit.md) - IP01 (#2588): per-source contract,
  licence, access, rate limits, revision model, the data-minimisation decision
  and the bounded first coverage. Written without network access; terms not
  re-verified live.
- Offline evidence: the pinned fixtures in `tests/fixtures/source_packs/society-*.json`
  and the revision fixtures in `tests/fixtures/income_distribution/`, replayed
  by `tests/unit/domains/test_income_distribution_*.py`.
- Live evidence: none yet. A dated live run is IP13 (#2648); until it exists
  every source is `unverified-live` and offline coverage is never reported as
  live coverage.
