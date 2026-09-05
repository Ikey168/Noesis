# Workflow policy, lifecycle and upgrade conformance

The integration matrix is in `tests/integration/workflow`. It uses temporary DuckDB warehouses and explicit synthetic sentinels. These checks localize behavior across stores; they do not assume that all stores share one policy engine.

## Current access matrix

| Surface | Reader/owner boundary | Restricted content and revocation | Historical behavior |
|---|---|---|---|
| Unified query | Current capability/source scopes; unauthorized adapters excluded | Filtering precedes merge, visible counts and locators | Cursor/catalog drift does not grant old access |
| Access views | Named principal, purpose, classification and policy version | Default deny; filter before counts/pages; invalidated projections | Old policies do not renew a share grant |
| Research snapshot | Token bound to owner; snapshot operation scope plus original data scopes | Inspect/bind/renew require current data access; stored scopes are not a grant | Expired metadata may be inspected with current access; expired tokens cannot execute queries |
| Saved subscription | Owner plus current namespace and operation scopes | New creation/evaluation captures a conservative union of non-subscription scopes; reads, listing, replay and delivery enforce them | Revoking a captured source scope denies retained payload access, including old cursors; legacy unbound rows retain the documented owner/namespace policy until evaluated |
| Research package | Explicit package capability; registered component accessibility | Inaccessible members are omitted and their private dependency graph is not traversed | A package already exported is an offline copy; local revocation cannot recall bytes held elsewhere |
| Portable namespace export | Explicit export capability and namespace access | Privileged namespace-wide export; caller supplies redaction policy | Export preserves registered snapshot content; it is not a live evidence query |
| Persistent projects/branches | Owner plus current namespace/domain scopes | Revoked source scopes prevent grounded evidence comparison | Reference-only history neither restores deleted bytes nor renews snapshot tokens |

Package and portable-export capabilities are privileged export permissions, distinct from recipient-specific access views. Their registered copies do not automatically inherit every source policy. Apply a validated access view/redaction before registering recipient-facing copies. The tests document this intentional boundary rather than asserting universal row-level policy propagation. IDs explicitly referenced by an authorized package request can appear in omission records; inaccessible descendants are not enumerated.

Captured subscription/snapshot scopes are deliberately conservative. An operator-created investigation can require operator access later, and unrelated data scopes present during capture can also restrict reopening. Create investigations with the intended reader's least-privileged scope set. Removing a subscription operation scope does not become a data grant. Owner transfer does not discard captured requirements.

## Lifecycle matrix

| Transition | Current evidence | Explicit retained history | Reclamation |
|---|---|---|---|
| Correction | Prior canonical revision stops qualifying immediately, before maintenance catches up | Pinned prior revision and derived lineage remain inspectable | Historical payload can be explicitly enrolled for policy-controlled reclamation |
| Retraction/deletion | No stale active claim or lexical/vector/graph/summary projection from the withdrawn support | Explicit generation reads preserve provenance; withdrawal is distinct from a query miss | Current active source payload cannot be reclaimed |
| Legal hold | Does not make withdrawn evidence active | Blocks enrolled payload reclamation | Releasing hold makes otherwise eligible objects collectible |
| Active snapshot pin | Does not override canonical current visibility | Protects enrolled canonical payloads referenced by pinned namespace/pack history | Expiration/closure removes the dynamic guard |
| GC | Does not restore current evidence | Identity, original hashes, changes and audit records survive | Reclaimed canonical payload reads raise `payload_reclaimed`; replay does not repeat deletion |

Current derived views consult committed canonical revisions before selecting candidates, so lagging maintenance cannot present old support as current. Legacy external references without a canonical ledger retain their explicit external-reference behavior. A claim with another current supporting source can remain active; stale citations are removed.

To enroll an immutable canonical payload, register a retention object with payload:

```json
{"managed_storage":{"kind":"document_revision_payload","document_id":"paper-1","revision_id":"document-revision:…"}}
```

The existing retention policy/plan/execute APIs then enforce minimum age, holds, dependency reachability and live snapshot pins. Candidate dependencies remain protected when a held parent is part of the same request. Execution recomputes eligibility and rejects stale/modified plans. Reclamation, its receipt, tombstone and audit publication share one transaction; injected failure rolls back the payload mutation. Pin/hold/source/GC API mutations are coordinated within the DuckDB writer process. Direct SQL writers must use an exclusive maintenance window.

This enrollment reclaims the canonical SQL revision payload, retaining a small marker and original hash metadata. It does not promise secure erasure of DuckDB historical disk pages, arbitrary external blob stores, previously exported packages, compatibility-table copies, or separately registered artifacts. Those copies have separate retention policies. Unenrolled objects retain the original retention-ledger tombstone semantics. Pack pins conservatively protect the pack's enrolled history, and namespace pins protect referenced source history up to their pinned generation.

## Storage compatibility

| Input | Upgrade behavior |
|---|---|
| Reviewed `0bf70327` schema | Synthetic fixture exercises document identity/revision, memberships, workflow receipts/resume, saved-query events, snapshot token/vector, package verification |
| Subscription storage v1/v2 | Add filter/delivery/access metadata; retain legacy hashes and owner/namespace access semantics |
| Pre-fingerprint membership state | Keep assignment/scan history; recompute derived fingerprint state on the next membership pass |
| Workflow storage contract v2 / subscription v3 | Idempotent reopening |
| Future/incompatible declared versions | Explicit failure before upgrade publication |

Stop the application writer and back up the warehouse, then run:

```sh
python scripts/upgrade_workflow_warehouse.py /path/to/warehouse.duckdb
```

The supported component upgrade is one transaction. Interruption before publication rolls back additive DDL; retry preserves identities and stage receipts. It does not convert arbitrary custom schemas or claim support for undocumented historical versions. Keep the backup for application rollback; downgrading an upgraded warehouse is not supported.

`tests/fixtures/workflow_upgrade/reviewed-base.json` records the reviewed module hashes and synthetic data produced with those older implementations. `scripts/make_workflow_upgrade_fixture.py` regenerates it from the pinned Git revision; unchanged shared dependencies use the installed repository runtime. Fixture tokens refer only to the synthetic warehouse. The migration test reopens the database, inspects prior state through public APIs, verifies the old package and resumes its interrupted workflow without losing receipts.
