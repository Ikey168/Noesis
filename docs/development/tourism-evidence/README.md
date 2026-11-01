# Tourism and hospitality evidence (economics.tourism)

Evidence for the Economics bundle's `economics.tourism` provider
(subdomain `tourism-hospitality`, track #2739, wave 2 tracker #2736).

- [source-audit.md](source-audit.md) - TO01: per-source contract, licence,
  access, rate limits, revision model, the data-minimisation decision and the
  bounded first coverage for Eurostat tourism occupancy and capacity; UN Tourism
  statistics recorded as `not-implemented` (no stable machine access). Written
  without network access; terms not re-verified live.
- [depth-audit.md](depth-audit.md) - DT01 (#2826, wave 4 tracker #2825):
  second-publisher audit under ADR-006. Recommends Destatis GENESIS (monthly
  accommodation survey, statistic 45412) for `economics.tourism` with the
  existing record shape; records that Destatis compiles the German figures
  Eurostat publishes (depth, not independence); OECD tourism statistics and
  Amt für Statistik Berlin-Brandenburg `not-implemented` this wave. Written
  without network access (2026-11-01); terms marked _verify_, no source live.
- Offline evidence: authored fixtures (fictional reference periods 2094-2097,
  release dates in 2024 so runtime retrievals never precede them; see the
  audit's deviation note): `tests/fixtures/source_packs/economic-tourism-*.json`
  (pinned in `config/source_packs/economic.json` 1.8.0) and the revision, NUTS
  version, correspondence and labour-link fixtures in `tests/fixtures/tourism/`.
- Offline acceptance: `tests/unit/domains/test_tourism_acceptance.py`
  (place to cited occupancy and capacity figures, sockets blocked) and the
  [guide](../../guides/economics-tourism.md). Status: covered offline only.
- Live evidence: none yet. Until a dated live run exists every Eurostat source is
  `unverified-live` and offline coverage is never reported as live coverage.
