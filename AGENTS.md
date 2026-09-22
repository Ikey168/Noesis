# Repository operating contract

This file applies to the complete Noesis repository.

- Read `README.md`, `CONTRIBUTING.md`, and the relevant architecture or contract
  document before changing a subsystem. Decisions in
  `docs/architecture/decisions/` remain binding until superseded.
- Run `mise run check` before declaring a core change complete. Optional-model,
  live-provider, browser, and large integration matrices remain explicit extra
  acceptance gates; do not claim they ran when only the common gate ran.
- Preserve dirty worktrees, source corpora, annotations, provenance, claims, and
  user-authored research. Do not rewrite history, push, publish, deploy, ingest
  external material, restore, or delete persistent data unless authorized.
- Never commit secrets, credentials, source-access tokens, private documents, recovery
  material, or production databases. Use synthetic fixtures and documented
  variable names.
- Treat virtual environments, caches, coverage, model caches, local warehouses,
  indexes, logs, and generated exports as generated. Irreplaceable source and
  provenance are data, not disposable build output.
- Cited answers must remain traceable to retained sources. Degraded capability
  and partial coverage stay explicit rather than being reported as success.
