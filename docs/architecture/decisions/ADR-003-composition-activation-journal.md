# ADR-003: Composition activation journal storage and startup reconciliation

Status: accepted. Part of C01 ([#1802](https://github.com/Ikey168/Noesis/issues/1802))
under [#1788](https://github.com/Ikey168/Noesis/issues/1788). Settles the two
decisions that [pack and workflow composition](../pack-workflow-composition.md)
defers to C01/C02. Facts come from the
[composition inventory](../pack-composition-inventory.md).

## Context

The lifecycle coordinator (C05) needs four pieces of durable state:

- composition selections (installed, selected, resolved)
- published composition generations with their plan digests
- an activation journal recording staged work
- an atomic pointer to the active generation

The architecture forbids a new database and duplicate source, ontology or
session stores. The existing candidates are:

| Candidate | Storage | Transactions | Fit |
| --- | --- | --- | --- |
| Pack registry (`src/domains/pack_registry.py`) | Filesystem directories `<root>/<name>/<version>/pack.json` | None; writes are individual files | Holds immutable published manifests. No atomic multi-row switch, no journal, and it is optional at runtime. |
| Domain registry / installer (`src/domains/registry.py`, `pack_install.py`) | Process memory | None | Holds what is live now, not durable state. It is the thing reconciliation must rebuild. |
| Source-pack store (`src/ingestion/source_packs.py`) | DuckDB warehouse tables (`source_pack_versions`, `source_pack_current`, audit) | `BEGIN` / `COMMIT` on the shared connection; immutable versions plus a current pointer | Has the exact pattern needed (immutable versioned rows, one mutable current pointer, audit). Its tables are source-pack-specific, and widening them would blur the source-pack authority. |
| Schema registry (`src/kb/schema_registry.py`) | DuckDB tables for contract modules, dependencies and lineage | Transactions and idempotency keys | The right home for composition *contract identities* (C02.6). The wrong home for selections and generations, which are deployment state rather than contracts. |

## Decision

1. **Storage.** Composition state lives in composition-metadata-only tables in
   the existing DuckDB warehouse, on the same connection as the source-pack
   store. It follows the source-pack store's pattern:
   - immutable versioned rows
   - one current-pointer row updated inside a transaction
   - an append-only audit and journal

   The tables are owned by one new module (`src/composition/`). They hold only:
   - selections
   - generations (plan JSON and digest)
   - journal entries
   - the active-generation pointer

   No manifest bodies are duplicated. Published manifests stay in the pack
   registry or under `packs/`; generations store content hashes plus the
   resolved plan.
2. **Atomic switch.** Publishing a generation is one transaction that inserts
   the generation row, marks its journal entries `published`, and updates the
   single active-pointer row. A failed publish leaves the previous pointer.
   Python registrations and external effects are *not* claimed to be atomic
   with it; they are staged before publish and reconciled afterwards.
3. **Startup reconciliation boundary.** At process start the coordinator:
   - *Reconstructs* in-process bindings from the active generation's plan:
     `DomainPack` registrations and enablement, and the catalog's plan-derived
     pack mapping. The plan is authoritative; `config/domain_packs.json` is
     read only for bundles still under legacy authority (C05.5).
   - *Reconciles* journal entries left `staged` by reading owner receipts, for
     example source-pack upgrade receipts and `source_pack_current`. An entry
     whose owner receipt shows success is marked `applied`; one with a failure
     receipt is marked `failed`; one with no receipt is marked `unknown`. An
     `unknown` entry is surfaced and not retried.
   - *Never* does any of the following: provider requests, acquisitions, source
     runs, schedule creation or deletion, license acceptance, or automatic
     publication of a new generation.

## Rejected alternatives

- **Extend the pack registry.** It is filesystem-based, has no transactions,
  and an atomic pointer across files would need its own locking and recovery.
- **Widen the source-pack tables.** A single authority per setting requires
  source-pack versions, cursors and schedules to stay owned by the source-pack
  store. Mixing composition generations into `source_pack_current` would give
  one table two meanings.
- **Store selections in the schema registry.** It versions *contracts*. Using
  it for mutable deployment selections would make every enablement change a
  module registration, with the wrong compatibility semantics.
- **A separate database or file.** Ruled out by the architecture. It would also
  split the transaction that must cover a generation switch from the source-pack
  receipts it reconciles against.

## Assumptions C05 may rely on

- One warehouse connection per process holds both the composition tables and
  the source-pack tables, so one transaction covers a pointer switch.
- Owner receipts are durable and idempotent by key (source-pack runs and
  upgrade receipts are today).
- Reconciliation is idempotent: running it twice produces the same state.
- Deployment is single-host. Distributed activation is deferred.

## Consequences

- C05.1 creates the tables and the selection states. C05.2 adds the journal,
  pointer switch and reconciliation.
- The catalog (C04) and the domain registry read the active generation. The
  legacy `config/domain_packs.json` remains authoritative only for bundles not
  yet cut over (C05.5).

## Amendment (C05, implementation)

Implemented in `src/composition/lifecycle.py` (`CompositionCoordinator`).

- **Installed documents.** C05.1 requires installing to *retain* the manifest and
  its hash so a restart can rebuild without the pack registry being present.
  `composition_installed` therefore keeps each installed manifest or provider
  descriptor as an immutable row keyed by `(kind, id, version, content_hash)`.
  Rows are never updated. Generations still reference them only by hash.
- **Tables.**
  - `composition_installed`, `composition_selections`, `composition_plans`
    (by digest), `composition_generations` and `composition_active` (a single
    pointer row).
  - `composition_journal` (append-only).
  - `composition_authority` (per-bundle cutover and compatibility-rollback marker).
  - `composition_provider_admin` (administrative shutdowns).
  - `composition_run_pins` (runs that pin a generation's providers).
- **Interrupted activations.** An activation with no `published` or `failed`
  entry is journaled `recovered/failed` at startup. Staged source-pack upgrades
  are settled from `source_pack_upgrade_receipts`: `applied` when the owner
  receipt exists, `unknown` otherwise. They are never re-run.
- **Destructive migrations.** Switching the generation pointer does not reverse
  a destructive provider schema migration. A provider migration ships its own
  compatibility and rollback plan through the schema registry migration
  surface. The generation switch only moves bindings.
- **Uninstall.** Uninstall removes installed documents and synthesized
  registrations that nothing references. Data deletion stays a separate
  retention operation.
