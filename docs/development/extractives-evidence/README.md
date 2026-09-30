# Extractives evidence

Evidence for the Economics `economics.extractives` provider (tracker #2653).

- [Source audit](source-audit.md) - EITI, USGS Mineral Commodity Summaries and
  BGS World Mineral Statistics contracts, the personal-data minimisation
  decision and the bounded first coverage (EX01, #2657). Written without live
  access to the providers; terms were not re-verified live.
- Offline evidence: the authored fixtures under `tests/fixtures/extractives/`
  and `tests/fixtures/source_packs/economic-extractives-*.json` (fictional
  companies and figures) replayed by `tests/unit/domains/test_extractives_*.py`.
- Live evidence: none yet. Bounded live acceptance is EX13 (#2717) and has not
  run; offline coverage is never reported as live coverage.
