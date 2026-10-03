# Tourism and hospitality evidence (economics.tourism)

Evidence for the Economics bundle's proposed `economics.tourism` provider
(subdomain `tourism-hospitality`, track #2739, wave 2 tracker #2736).

- [source-audit.md](source-audit.md) - TO01: per-source contract, licence,
  access, rate limits, revision model, the data-minimisation decision and the
  bounded first coverage for Eurostat tourism occupancy and capacity; UN Tourism
  statistics recorded as `not-implemented` (no stable machine access). Written
  without network access; terms not re-verified live.
- Offline evidence: authored fixtures (fictional reference periods 2094-2097,
  release dates in 2024 so runtime retrievals never precede them; see the
  audit's deviation note): `tests/fixtures/source_packs/economic-tourism-*.json`
  (pinned in `config/source_packs/economic.json` 1.8.0) and the revision, NUTS
  version, correspondence and labour-link fixtures in `tests/fixtures/tourism/`.
- Live evidence: none yet. Until a dated live run exists every Eurostat source is
  `unverified-live` and offline coverage is never reported as live coverage.
