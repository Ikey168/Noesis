# Backlog pull request validation — 2026-09-09

The accumulated backlog was committed on `feat/backlog-investigation-workflows`
and integrated with `origin/main` at `32d736ed` before publication. Earlier
implementation receipts describing uncommitted work remain historical snapshots.

Merge resolutions preserve both ORCID public APIs, both sentence segmentation
hooks with source-offset validation, the language candidate configuration alias,
LightOn/MarkItDown PDF adapters alongside bounded PDF validation, and batched
query-specific embeddings. The combined MCP catalog was regenerated.

Validation after integration:

- Ingestion, evaluation, integrations, knowledge storage, MCP host, affected
  retrieval tests and the public investigation workflow: 1,742 passed, 27 skipped,
  two failures in source-inventory count assertions.
- The combined inventory contains 25 sources and five configured research sources.
  After updating those expectations, both affected source-pack suites plus four
  new backend compatibility regressions passed: 31 passed in 2.63 seconds.
- Changed Python files parse, generated contracts are covered by the MCP-host
  suite, and staged/unstaged whitespace checks pass.
- Broader RAG collection stopped because the environment lacks `mlflow`; that
  attempt is not counted as passing validation.

Local logs: `/home/ik/.cache/noesis-pr-merge-verified.log` and
`/home/ik/.cache/noesis-pr-merge-final.log`.

Independent human annotations, further quality evaluations and provider access
remain deferred as documented in [the follow-up plan](further-evaluations-and-access.md).
The local `config/blog_subscriptions.http.sqlite` cache is excluded from the PR.
