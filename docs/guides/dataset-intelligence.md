# Dataset intelligence

Noesis treats datasets as versioned knowledge sources rather than anonymous
files. The dataset catalog assigns stable identities to publishers, native
datasets, tables, columns, dimensions, code lists, partitions, and licenses.
Catalog revisions are append-only: a declared column rename preserves the
column identity, while a schema change produces a new revision with an explicit
predecessor.

## Releases and observations

A release pins its native release identifier, vintage, publication and
retrieval times, provenance, and optional correction target. Observations are
addressed through a release, table, and partition, so two vintages can be
compared without silently replacing history. Cells retain explicit null
semantics such as `suppressed` and `not-applicable`; Noesis never invents a
value for a suppressed cell.

Large tables are accessed with bounded, deterministic slices. Release
comparison reports changed rows against their stable primary keys. This layer
complements the quantitative semantic layer: it preserves source-native tables
and releases, while metric normalization and transformation remain explicit in
the semantic layer.

## Bounded ingestion

The ingestion boundary accepts CSV, JSON, JSON Lines, Parquet, and tabular API
responses. Operators control the encoding, row limit, and inference limit;
Noesis also caps inferred schemas at 100 columns and ingestion at 10,000 rows
per call. Typed normalization records malformed rows in quarantine, reports
schema drift, and returns deterministic receipts that can be replay-checked.
Cancellation produces a cancelled receipt without committing observations.

Receipts include source and normalized content hashes, counts, inferred fields,
drift, partition identity, and provenance. Reusing a release/partition identity
with conflicting content is rejected rather than overwriting prior data.

## Join safety and lineage

Join discovery proposes keys from stable column identities, shared code lists,
and semantic roles. Suggestions are advisory. A bounded preview must be run
before acceptance and reports cardinality plus warnings for many-to-many joins,
unit mismatches, temporal-frequency mismatches, and possible namespace leakage.
Accepting a preview records its exact transformation, inputs, derived table
identity, producer, and preview hash as queryable lineage.

The MCP surface separates permissions into:

- `knowledge:dataset:read` for catalog search, schemas, slices, comparisons,
  suggestions, replay checks, and lineage;
- `knowledge:dataset:write` for catalog and release registration and accepted
  joins;
- `knowledge:dataset:ingest` for source parsing and observation commits; and
- `knowledge:dataset:calculate` for bounded join previews.

All list and data endpoints are paginated and budgeted. Offline conformance
fixtures exercise economic and scientific datasets, authorization failures,
format errors, schema evolution, corrections, and cross-namespace isolation.
