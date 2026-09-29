# Products expansion: evidence

Offline and live evidence are kept apart.

- **Offline** (fixtures, no network, no credentials): the source audit
  ([source-audit.md](source-audit.md)) and the tests
  `tests/unit/domains/test_product_categories.py`,
  `tests/unit/domains/test_products_expansion_sources.py`,
  `tests/unit/domains/test_products_expansion_acceptance.py` and
  `tests/unit/composition/test_products_expansion_composition.py`. Fixtures are
  authored envelopes and catalogues for fictional brands and manufacturers.
- **Live** (dated, bounded): none yet. Every new source stays
  `unverified-live` until PX12 (#2104) records a dated run here with per-selector
  outcomes, response hashes and failure codes.

| Run | Result |
| --- | --- |
| none yet | No dated live run exists for the appliance groups or component sources. |
