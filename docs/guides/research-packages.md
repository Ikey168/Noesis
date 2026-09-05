# Portable research packages

## Verification and import trust

Verification checks member hashes and required roots/dependencies, the closure
digest, and completeness declarations. An undeclared missing member fails even
if the outer checksum has been recalculated. Bounded partial exports declare
their unvisited frontier. `structural_errors` explains structural failures
separately from `member_failures` and `signature_status`.

`set_research_package_trust_policy` requires `knowledge:packages:trust` and appends
an import-namespace policy using an expected revision (zero for creation). Each
trusted key record contains `public_key` (base64 Ed25519 public bytes),
`key_version`, and optional `revoked`. Use new key IDs during overlapping key
rotation, or advance the explicitly accepted version.

Importers may request `require_signature` and provide `public_keys` when there is
no namespace policy. A configured namespace policy is authoritative: importers
cannot bypass its signer requirements or substitute their own trusted keys.
Policies are checked before idempotent import replay, so revocation also applies
to repeated imports. Namespaces without a policy still permit unsigned exchange.

## Package contents and execution

A research package binds the question, plan, committed snapshot, evidence,
transformations, findings, limitations, policies, and compatibility metadata
needed to reproduce a result. Manifests are canonical and version-negotiated;
unknown top-level fields fail validation, while namespaced `x-` extensions can
travel without changing the core contract.

Dependency closure walks documents and revisions, claims, datasets, methods,
models, ontologies, policies, recipes, and content-addressed assets. Shared
dependencies occur once in canonical order. Inaccessible members become
explicit omissions and policy-approved members may carry redacted content;
neither state is silently presented as a complete package.

Unencrypted package bytes are deterministic and addressed by SHA-256. Optional
Ed25519 signatures identify both key ID and rotation version. AES-256-GCM
envelopes authenticate recipient-bound offline packages and also carry a key
version. Large assets should remain content-addressed references, keeping the
manifest bounded. Verification checks the package hash, every member hash,
missing members, trust key, and signature fully offline.

Imports require a namespace beginning with `import:`. They are atomic,
idempotent, reject identity collisions, and never overwrite ordinary local
knowledge. Recipe members are non-executable unless their exact IDs are trusted
at import and explicitly enabled during replay. Rollback removes the isolated
members while retaining the import audit receipt.

MCP operations use `knowledge:packages:read`, `knowledge:packages:write`, and
`knowledge:packages:import`. Private encryption and signing keys are used only
for the immediate operation and are never persisted.
