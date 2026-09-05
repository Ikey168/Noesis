# Knowledge retention, compaction, and archival

Retention policies are immutable and versioned. Rules may inherit from a parent
and constrain eligibility by object class, namespace, source license, age,
value, access class, and generation. Snapshot, session, evidence-bundle, and
export pins remain explicit. Finite and indefinite legal holds override normal
eligibility; dry runs explain every blocking reason.

Compaction produces content-addressed checkpoints over bounded generation
ranges. The hash covers schema version, records, and tombstones, so histories
remain replayable across schema upgrades and corruption is detectable.
Interrupted compaction is recorded as cancelled rather than partially visible.

Archive writes use `{"driver":"filesystem","uri":"/configured/archive.json"}`.
The file contains the manifest, records, and tombstones. Publication fsyncs a
temporary file, links it into place without overwriting another archive, fsyncs
the directory, and verifies the persisted bytes before reporting `archived`.
The returned manifest includes the byte count and SHA-256 checksum. Keep that
manifest separately for recovery. Archives are bounded to 64 MiB and checkpoints
to 1,000 records and 1,000 tombstones; larger inputs require explicit splitting.

`restore_knowledge_archive` accepts the saved manifest when the original database
is unavailable. It retrieves the actual file, checks byte and checkpoint identity,
validates the archive format and supported checkpoint schema version, then restores
checkpoint records, tombstones, archive receipt, and audit in one transaction.
These are the generic records supplied to `create_retention_checkpoint`; restoration
does not infer arbitrary application-table mutations from untyped record fields.

Legacy metadata-only archives cannot be restored and must be republished from their
checkpoint. The old `partial=true` simulation is rejected; partial/unavailable outcomes
come from I/O. Encryption declarations are rejected until an encrypted byte backend
is configured in the implementation, preventing plaintext from being labelled encrypted.
The cancellation and explicit `storage_available=false` opt-out remain supported.

The backend interface is `write(storage, bytes)` and `read(storage)`, injectable for
fault testing. Only the local filesystem backend ships here. We evaluated
[fsspec transactions](https://filesystem-spec.readthedocs.io/en/latest/features.html#transactions):
their commit behavior is backend-specific and does not establish cross-instance atomicity.
A future remote adapter must prove durability and checksum semantics for its chosen
backend; adding fsspec alone would not meet those requirements.

Garbage collection is a two-step plan and execute operation. Planning checks
age/value policy, all pins and legal holds, and reverse transitive ownership by
retained artifacts. Execution rechecks object guard hashes to catch races with
new pins or holds. Failed deletion changes nothing; successful execution creates
logical tombstones and is replay-safe.

MCP scopes are `knowledge:retention:read`, `knowledge:retention:admin`, and
`knowledge:retention:execute`. Administrative and execution mutations are
audited and namespace-bound.
