# AI models and datasets evidence (technology.ai-models)

Evidence for the Technology bundle's `technology.ai-models` provider (wave 2
tracker #2736, track #2742, subdomain `ai-models-datasets`).

- [source-audit.md](source-audit.md) - AI01: per-source contract, licence,
  access, rate limits, revision model, the data-minimisation decision and the
  bounded first coverage. Written without network access; terms not
  re-verified live. Hugging Face Hub metadata, OpenML and the Epoch AI models
  dataset are `unverified-live`; gated repositories, leaderboards and Papers
  with Code are documented, not acquired.
- Machine-readable copy: `src/ingestion/ai_models_sources.py`
  (`PROVIDER_CONTRACTS`, `BOUNDED_COVERAGE`, `CAPS`, `MINIMISATION`,
  `EXCLUSIONS`, `LIVE_VERIFICATION`), matching the audit (checked by
  `tests/unit/domains/test_ai_models_sources.py`); source pack
  `config/source_packs/technology-ai-models.json` with every source at
  `live_verification: unverified-live`.
- Offline evidence (AI02-AI12): authored fixtures for fictional organisations
  and repositories (`example-org/fixture-model`, invented shas, OpenML ids and
  Epoch rows, dates 2094-2099) in `tests/fixtures/source_packs/technology-ai-models-*.json`
  and `tests/fixtures/ai_models/`, replayed through the real adapter. The
  offline acceptance journey is
  `tests/unit/domains/test_ai_models_acceptance.py`; the guide is
  [technology-ai-models.md](../../guides/technology-ai-models.md). The
  subdomain is covered offline in `packs/taxonomy.json`, not live.
- Live evidence: none. The provider hosts were blocked by the runtime's egress
  proxy; no live run has happened. Until a dated live run (AI13) exists every
  source is `unverified-live`, and offline coverage is never reported as live
  coverage.
