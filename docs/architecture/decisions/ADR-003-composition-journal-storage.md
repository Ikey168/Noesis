# ADR-003: Composition journal storage and startup reconciliation

Status: accepted. Decision record for C01.5
([#1802](https://github.com/Ikey168/Noesis/issues/1802)) of the
[pack and workflow composition architecture](../pack-workflow-composition.md).
C05 builds on it without reopening it.

## Context

The architecture needs durable composition state: installed composition
manifests, root selections, resolved plans, an activation journal and an
active-generation pointer. It defers two decisions to C01/C02: where that state
lives, and what process startup reconstructs and reconciles.

Three existing registries were evaluated for extension.

| Candidate | Transaction and lifecycle contract | Fit |
| --- | --- | --- |
| Pack registry (`src/domains/pack_registry.py`) | Filesystem tree of immutable `pack.json` versions. No transactions, no journal, no per-deployment state. | Rejected. It is a publication index shared across deployments, not deployment state. An atomic pointer switch on a filesystem tree would need its own locking. |
| Source-pack store (`src/ingestion/source_packs.py`) | DuckDB tables on the warehouse connection, explicit `BEGIN`/`COMMIT`, audit rows, immutable versions and receipts. | Its transaction style fits, but its tables are keyed by source `pack_id` and govern acquisition. Adding composition rows to them would make source packs the owner of pack lifecycle. |
| Schema registry (`src/kb/schema_registry.py`) | DuckDB tables, immutable content-addressed modules, idempotency keys, lineage events. | Right owner for contract identities, which it now holds. Wrong owner for deployment selections and activation state. |

A separate database was ruled out by the architecture: no duplicate source,
ontology or session store.

## Decision

**Storage.** Composition metadata lives in new `composition_*` tables on the
same DuckDB warehouse connection the source-pack store uses. The tables hold
only composition metadata: retained manifests and provider descriptors, root
selections, cutover markers, resolved plans by digest, the activation journal,
activation receipts, and a single-row active-generation pointer. They follow
the source-pack store's conventions: canonical JSON columns, content hashes,
explicit transactions, and an audit trail. The module is
`src/composition/store.py`.

Contract identities stay in the schema registry. Source pins, cursors,
schedules and receipts stay in the source-pack tables and are referenced by
`(pack_id, version, manifest_hash)` and receipt IDs, never copied.

**Atomicity.** The generation switch is one `UPDATE` of the pointer row inside
a transaction that also writes the `published` journal stage. That makes the
pointer and journal consistent with each other. It does not make Python
registrations or external actions atomic. In-process registrations are derived
from the active generation and rebuilt on demand, so a crash between the
database commit and the in-process apply is repaired by reconciliation.

**Startup reconciliation boundary.** Reconciliation runs once per process after
the legacy `load_config()` in `src/api/app.py`. It:

1. Reads the active generation and rebuilds its in-process registrations and
   provider bindings. With an empty journal and a published generation this
   reproduces identical bindings.
2. Marks every journal entry left in `previewed`, `staged` or `verified` as
   `abandoned`. The previous active generation stays active.
3. Reconciles staged source-owner operations only by reading their owner
   receipts (`SourcePackUpgradeStore` receipts). A receipt that exists is
   recorded against the abandoned entry; a missing receipt is recorded as
   `not-applied`.

Reconciliation never issues provider requests, never runs acquisition, and
never creates, enables or changes schedules.

## Assumptions C05 may rely on

- One warehouse connection per deployment; one active generation per
  deployment. Multi-host coordination is deferred by the architecture.
- The pointer row exists at most once and is only written in a transaction
  together with a `published` journal stage.
- Journal stages are append-only rows keyed by activation ID and stage.
- A generation's registrations are a pure function of its stored plan and
  manifests.
- Legacy registrations for bundles that are not cut over are untouched by
  reconciliation.

## Consequences

- Composition state is backed up and restored with the warehouse, alongside
  the source-pack receipts it references.
- A deployment without a warehouse connection keeps the legacy lifecycle; the
  coordinator is not installed and legacy calls behave as before.
- Switching the pointer back does not reverse a destructive provider data
  migration. Such migrations need their own compatibility and rollback plan.
