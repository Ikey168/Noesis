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

Archive manifests identify the checkpoint, pluggable storage driver/location,
encryption metadata, and checksum. Partial, unavailable, and cancelled archive
attempts are explicit. Restore verifies identity before changing the manifest
to `restored`, providing atomic visibility.

Garbage collection is a two-step plan and execute operation. Planning checks
age/value policy, all pins and legal holds, and reverse transitive ownership by
retained artifacts. Execution rechecks object guard hashes to catch races with
new pins or holds. Failed deletion changes nothing; successful execution creates
logical tombstones and is replay-safe.

MCP scopes are `knowledge:retention:read`, `knowledge:retention:admin`, and
`knowledge:retention:execute`. Administrative and execution mutations are
audited and namespace-bound.
