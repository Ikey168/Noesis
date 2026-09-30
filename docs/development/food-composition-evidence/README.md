# Food composition evidence (#2216)

Evidence for the Products `food` feature (food composition and labelling):

- [source-audit.md](source-audit.md) - FC01 access, licence and rate-limit contracts for Open Food Facts (ODbL
  obligations), USDA FoodData Central and the composition tables, the crowd-sourced / reference provenance
  decision and the bounded coverage.
- Offline evidence: `tests/unit/domains/test_food_composition_*.py` replay the synthetic fixtures under
  `tests/fixtures/source_packs/products-food-*.json` through the real adapters and runtime.
- Live evidence: none yet. Every provider is `unverified-live`; the dated live run and cited demo belong to #2302
  and are reported separately from the offline results.
