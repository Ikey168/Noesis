# Access-bound knowledge views

Access-bound views make authorization part of knowledge retrieval rather than a
post-processing filter. Versioned policies default to deny and may constrain a
principal, purpose, namespace, classification, source license, jurisdiction,
and allowed transformation. Decisions run before ranking, traversal,
aggregation, counting, and pagination, preventing result-shape side channels.

Objects retain their existing identity and payload; the view registry only
binds policy metadata to that source of truth. Redacted summaries, claims,
indexes, embeddings, and bundles are separate, policy-versioned projections.
Their lineage uses opaque references so a permitted derivative cannot disclose
a restricted input. A newer policy version invalidates older projections.

Portable exports require a grant matching recipient, purpose, object set,
expiry, redistribution rights, and watermark rules. Revocation takes effect at
the next authorization check, including for offline-package creation.

Ordinary denials return `not_available` without disclosing existence or policy
details. Effective-view explanations, simulations, health, and the access audit
require `knowledge:views:admin`; reads, registration/projection writes, and
exports use `knowledge:views:read`, `knowledge:views:write`, and
`knowledge:views:export`, respectively.
