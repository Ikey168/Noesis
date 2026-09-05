# Unified knowledge query

Noesis exposes one bounded query contract across domain backings, temporal
history, scoped memory, and explicitly enabled federated sources. The planner
is deterministic: it records every selected source, authorization or policy
omission, capability hash, dependency, and per-source budget before execution.

The three public contracts are:

- `noesis-knowledge-query-request-v1`
- `noesis-knowledge-query-plan-v1`
- `noesis-knowledge-query-result-v1`

Contract versions are additive within `v1`: new optional fields and enum values
may be introduced, while existing required fields retain their meaning. A
required-field removal, semantic reinterpretation, or evidence-boundary change
requires a new contract version. Valid engine output and a deliberately invalid
request live under `tests/fixtures/unified_query/`.

## MCP workflow

1. Call `unified_query_capabilities` to discover authorized surfaces.
2. Call `explain_knowledge_query` to inspect selection, omissions, and budgets.
3. Call `query_knowledge` with the same request. Supply a stable `query_id` if
   another client may call `cancel_knowledge_query`.
4. Use `replay_knowledge_query` while its process-local replay record exists,
   or persist the returned request, plan, and replay hash in the caller.
5. Call `evaluate_knowledge_query` for structural retrieval metrics.

Remote sources are excluded unless `source_policy.allow_remote` is true and
the principal has the adapter's scopes. Required sources fail closed. Optional
source failures produce a partial result with typed failure and coverage data.
Existing specialized MCP tools remain available; clients can migrate by mapping
their domain to `scope.domains`, their retrieval kind to `surfaces`, and their
limit to `budgets.max_results`, then comparing the explained plan before moving
execution to `query_knowledge`.

## Evidence and memory

Results merge only on canonical identity or shared origin identity—not similar
text. Native scores and ranks are retained, while reciprocal-rank fusion gives
a stable cross-source ordering. Citations, provenance, temporal bounds,
contradictions, and source independence remain visible.

Memory modes are `off`, `query-expansion`, and `separate`. Memory is always
labelled `context-only`; it never appears in an item's evidence list and never
increases its independent-source count. This prevents remembered preferences
or summaries from being mistaken for factual corroboration.

## Offline conformance

The total query deadline includes memory expansion, worker admission, local lock
waits, and retries. Each retry receives only the remaining time; adapters must
apply that timeout to their I/O. Slow optional sources return typed partial
failures, while required-source failure remains an error.

The process has eight shared query workers and no unbounded submission queue.
Saturated providers return `source_busy`. The caller does not wait for executor
shutdown after a timeout. An already-running, non-cooperative provider may finish
later, occupying one bounded slot; its late result is discarded. Cancellation
does not forcibly terminate Python threads. File-backed local adapters receive
independent DuckDB connections so request cleanup cannot close an in-flight
reader. In-memory integrations must keep their parent connection alive until
their provider returns.

Runtime timeout allocations are excluded from semantic query hashes, preserving
cursor replay when the result set is unchanged.

```bash
make unified-query-check
```

The fixture checks deterministic planning and replay, source failures,
provenance and citation preservation, result bounds, and memory/evidence
separation without network access.
