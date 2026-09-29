# Natural Hazards evidence

Evidence for the Natural Hazards pack (#2207), kept separate by kind:

- `source-audit.md` — NH01 (#2304): per-source contract, licence, rate-limit and revision audit,
  access decisions and the bounded first coverage.
- **Offline evidence** — authored fixtures under `tests/fixtures/hazards/` (fictional values dated
  2099) replayed through the real adapters by `tests/unit/hazards/` and
  `tests/unit/domains/test_natural_hazards_acceptance.py`. Offline results never count as live
  coverage.
- **Live evidence** — none yet. Every source is `unverified-live` until a dated, bounded live run is
  recorded here (NH15, #2375, needs live access and human review).
