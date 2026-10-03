# AI models and datasets evidence (technology.ai-models)

Evidence for the Technology bundle's `technology.ai-models` provider (wave 2
tracker #2736, subdomain `ai-models-datasets`).

- [source-audit.md](source-audit.md) - AI01: per-source contract, licence,
  access, rate limits, revision model, the data-minimisation decision and the
  bounded first coverage. Written without network access; terms not
  re-verified live. Hugging Face Hub metadata, OpenML and the Epoch AI models
  dataset are `unverified-live`; gated repositories, leaderboards and Papers
  with Code are documented, not acquired.
- Machine-readable copy: none yet. It will be added in
  `src/ingestion/ai_models_sources.py` by the track's acquisition issues and
  must match the audit.
- Offline evidence: none yet. Fixtures will be authored for fictional
  organisations and repositories with dates in 2094-2099.
- Live evidence: none. Until a dated live run exists every source is
  `unverified-live`, and offline coverage is never reported as live coverage.
