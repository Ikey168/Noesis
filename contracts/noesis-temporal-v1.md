# noesis-temporal-v1 — bitemporal knowledge contract

Noesis keeps two independent clocks for knowledge. **Valid time** describes
when an assertion applies in the world; **observation time** describes when
Noesis learned or ingested it. The temporal projection is additive to the
source stores and is served through `kb_temporal` over both MCP and REST.

## Assertion model

Every immutable temporal assertion records:

- `valid_from_ms` / `valid_to_ms`: an optional, half-open world-time interval
  `[from, to)`;
- `observed_at_ms` / `ingested_at_ms`: the inclusive system-time axis;
- `retracted_at_ms`: when Noesis learned the assertion was withdrawn;
- the original object identity and payload, source document, visibility, and
  backing;
- whether time was source-reported or inferred, its precision, approximation
  status, and parsing provenance.

Unknown/open/approximate time is represented explicitly rather than replaced
with false precision. A valid-time query excludes assertions whose valid time
is wholly unknown and reports that exclusion in `coverage_limitations`.
Documents, claims, entities, relations, and dataset observations use the same
storage contract; governed fixtures cover every externally returned kind.

## Ingestion normalization

The normalizer recognizes publication, effective, event, revision, correction,
retraction, and ingestion timestamps. It retains original timestamp text,
source field, source-timezone assumptions, detected precision, approximation
status, parser name, and parser version. Naive source times use a declared
`source_timezone`, falling back to UTC with the assumption recorded.

Malformed timestamps and impossible intervals are retained in
`kb_temporal_quarantine`; they never become invented dates. Publication/event
fixtures exercise papers, legislation, filings, datasets, news, and uploads.

## Query semantics

`as_of` is shorthand for setting both axes. Explicit `valid_at` and
`observed_before` each override the corresponding `as_of` value. Observation
cutoffs are inclusive. Snapshot mode returns the latest known version per
object at the cutoff while preserving equally recent conflicts; `history=true`
returns every matching immutable version. Retracted assertions are hidden from
current snapshots by default but remain available through history or
`include_retracted=true`.

Opaque cursors bind to the complete query and freeze its observation cutoff,
so later ingestion cannot shift subsequent pages. Limits are 1–100. The
response always names effective clocks, interval rules, backing, migration
receipt, and known coverage limitations.

## Revision relationships

Revision-aware consolidation distinguishes:

- `supersedes`: a later assertion from the same source replaces an earlier
  incompatible assertion;
- `corrects`: the replacement is an explicit correction notice;
- `retracts`: the source withdraws the earlier assertion;
- `contradicts`: independent sources disagree, or same-source assertions are
  contemporaneous and ordering cannot resolve the conflict.

Both old and new claims remain addressable. Transition assertions retain their
claim/document evidence endpoints, while presentation layers avoid selecting a
superseded, corrected, or retracted claim when a live member exists.

## Migration and access control

`kb_document_times`, `kb_temporal_assertions`, and quarantine storage are
additive DuckDB tables with domain/observation, object, and valid-time indexes.
The projection backfills lazily and idempotently from either corpus-view or
namespace backings. Missing legacy time becomes an explicitly inferred default;
object IDs do not change during promotion or reversal. Because source tables
are untouched, the projection can be discarded and rebuilt from the active
backing where the deployment's migration framework permits rollback.

Authorization is resolved before materialization or query. Public and private
domains never share a query scope; private domains require an authenticated
principal, `include_private=true`, and an explicit domain grant.

Schemas: `noesis-temporal-assertion-v1`, `noesis-temporal-query-v1`, and
`noesis-temporal-response-v1`.
