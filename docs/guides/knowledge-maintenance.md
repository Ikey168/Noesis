# Continuous knowledge maintenance

Noesis can continuously move enabled source-pack schedules through normalized
documents, the versioned Knowledge Engine workflow, selective derived-artifact
refresh, an append-only generation commit, and the unified query plane. The
worker uses a versioned plain-text extractor and the configured semantic embedding
provider in production mode. Source access remains offline by default; live source
network access requires an explicit flag. Missing model dependencies produce an
explicit unavailable result instead of silently emitting fixture knowledge.

## Bootstrap

Install and enable the desired source packs, accept each source's terms, and
create bounded interval schedules with the Knowledge Engine MCP tools. Then run
one foreground tick:

```bash
python scripts/knowledge_maintenance_worker.py --once
```

Run the deterministic six-domain conformance path without credentials or
network access:

```bash
make knowledge-maintenance-check
```

The reference covers all 6 packs and 22 sources, commits both successful and
degraded generations, verifies every receipt, and proves the committed
documents are queryable.

## Revisions and deltas

Every normalized source identity has an immutable revision chain. Repeated
observations are recorded as `unchanged` delta entries without creating a new
revision; metadata edits, corrections, retractions, and explicit tombstones
append distinct revisions with predecessor links. Existing warehouses are
seeded deterministically at revision zero when the document store opens.

A successful source run commits its `noesis-document-change-set-v1` in the
same transaction as its source watermark and receipt. Consumers can use
`document_generation_delta` for bounded, opaque-cursor paging,
`replay_document_generation_delta` to verify it against stored revisions, and
`document_revision` / `document_revision_history` for exact-generation or
point-in-time reads. These MCP reads require `knowledge:read`; no revision API
can expose staged rows.

Maintenance consumes consecutive committed deltas rather than looking for a
mutable run marker in current document metadata. Projection dependencies name
exact revision IDs, so edits and removals invalidate their downstream closure
while unaffected revision dependencies remain stable.

## Derived knowledge truth maintenance

Committed document deltas also feed an authoritative derived-object ledger.
Claims, entities, relations, per-document summaries, lexical entries, and
embedding inputs receive stable logical identities plus immutable revisions.
Each revision records its exact source-document revisions, producer and
configuration identity, observed time, publication generation, and lifecycle.

Support is tracked independently from object content. If two documents support
the same normalized claim and one is corrected or removed, Noesis appends a
`support_updated` revision and keeps the claim active. It appends a retraction
only after the final support disappears. Reappearance creates a `restored`
revision rather than destroying the earlier history.

Lexical, provider-backed vector, graph, and summary projection items update in
the same transaction as the derived revision and generation receipt. Readers
therefore see either the previous projection generation or the complete next
one, never a mixture. The existing aggregate artifacts remain compatibility
manifests and carry the derived generation receipt in maintenance output.

Read-only MCP tools expose exact or as-of revisions, revision history, bounded
generation deltas, replay verification, source-to-projection lineage,
invalidation explanations, current projections, and text-free health counts.
All require `knowledge:read`, and queries cannot observe an uncommitted derived
generation.

## Extractors and vector spaces

`config/knowledge-maintenance.json` selects `execution_mode` (`production` by
default). An optional `extractor_definition` chooses a registered name, semantic
version, model/rule version, output schemas, configuration, and resource declaration.
The built-in `argument-mining-text` implementation uses the existing claim-mining
API on ordinary `document.content`. Outputs retain source revision provenance and
exact character locators where the claim has a unique exact source span. Custom
implementations are injected into `MaintenanceOrchestrator`; an unbound registered
definition fails explicitly.

Embedding configuration uses the existing `EMBEDDING_PROVIDER` and
`EMBEDDING_MODEL_NAME` settings. `embedding_configuration` records tokenizer
revision and input policy. Vector receipts include model, dimension, tokenization,
and configuration identity. A provider change cannot mix spaces within an active
namespace; rebuild into a new namespace. Maintained semantic queries are registered
for selected namespaces in the unified query catalog and retain source-revision
citations. Derived object summary vectors retain an explicit character-prefix
policy (4,000 by default). Production maintenance additionally indexes full source
documents in tokenizer-bounded chunks. Domain and namespace document searches rank
these chunks and return their original character offsets and source revision IDs.
The local Sentence Transformers backend counts special tokens within its model
limit. A backend without an explicit tokenizer contract fails instead of silently
truncating input. Indexing bounds documents, chunks, and inference batches; an
overflow is explicit. Completed batches survive retry, while publication receipts
join the maintenance generation transaction. Missing or stale chunks produce
partial coverage in unified queries.
Historical snapshot queries currently return an explicit unsupported outcome on
this semantic adapter instead of reading newer vectors.

Set `execution_mode: fixture` only for deterministic offline conformance. That mode
uses `metadata.knowledge` and explicitly synthetic hash vectors. The reference
script selects it directly. Synthetic vectors are rejected by semantic search and
are never a production fallback.

## Operation and scaling

Run `scripts/knowledge_maintenance_worker.py` without `--once` for a foreground
worker, or install `deploy/systemd/noesis-maintenance.service`. Configuration is
in `config/knowledge-maintenance.json`; credentials remain environment-backed
and are never stored there. Multiple workers may share the warehouse. Durable
leases, owner IDs, and monotonically increasing fencing tokens prevent two
workers from executing the same pack concurrently. Polling, catch-up, jobs per
tick, retries, and lease duration are bounded.

Live sources are opt-in:

```bash
python scripts/knowledge_maintenance_worker.py --live-network
```

Use separate worker processes for throughput. DuckDB still serializes writes,
while pack-level exclusion ensures a later interval never overtakes an active
one for the same pack.

## Recovery and visibility

Expired leases become retryable (or dead-letter after the attempt budget), and
unfinished source and workflow work resumes from stable run keys and receipts.
The worker advances a schedule only after a generation commits. Queries through
the maintained-document adapter consider only source runs referenced by a
committed end-to-end generation, so an interrupted workflow cannot leak a mixed
generation.

MCP read controls list due work and jobs, inspect attempts and generations,
verify replay, traverse lineage, and report health. Operator controls run one
job or a bounded drain. Administrative controls pause/resume schedules and
cancel or retry jobs. Audit records contain stable error classifications and
never raw credentials or quarantined payloads.

## Troubleshooting

- `credential_missing`, `license_not_accepted`, and `network_policy` are policy
  gates; correct operator configuration rather than bypassing them.
- `retry` means a bounded backoff remains. `dead-letter` means the attempt
  budget is exhausted and requires an explicit retry after fixing the cause.
- A degraded generation records optional-source omissions. Required-source
  failures do not commit a generation.
- Check `maintenance_health` for schedule/processing lag, stale leases, stuck
  workflow stages, stale sources, freshness, recovery time, and the last commit.
- Use `replay_maintenance_generation` before investigating data drift; it checks
  source receipts, workflow watermark/state, change event, and receipt hash.
