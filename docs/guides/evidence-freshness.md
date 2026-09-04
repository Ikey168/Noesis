# Evidence freshness and decay

Noesis treats freshness as an explainable property of evidence, not as a deletion policy. Evidence identities and their original provenance remain immutable while assessments record whether that evidence is fresh, nearing expiry, stale-but-valid, expired, timeless, unknown, or invalid at a specific instant.

## Lifecycle model

A versioned policy is selected by namespace, domain, source type, and object type. Exact selectors take precedence over `*` wildcards. Rules may define maximum age, warning windows, expected update cadence, event-closure grace, decay half-life, required source health, missing-date behavior, and an expected methodology revision. Registering a new semantic version can supersede the active version without rewriting prior policy or assessment records.

Freshness annotations reference the evidence IDs already owned by Noesis subsystems. They preserve published, observed, retrieved, valid-time, event-closure, methodology, source-health, generation, producer, policy, and provenance metadata; they do not create a second evidence store.

Applicability relations connect old and new evidence with one of `supersedes`, `narrows`, `invalidates`, or `no-longer-applies`. Each relation carries its own evidence, provenance, confidence, valid/observed time, and optional applicability fields such as `fraction` and `jurisdiction`. Partial and jurisdiction-specific changes are retained in the explanation rather than collapsed into a global invalidation. Multiple applicable successors are explicitly reported as conflicting.

## Assess and propagate

`assess_evidence_freshness` creates an append-only, hash-addressed assessment. Its reasons show the policy boundary, source cadence and health, methodology, applicable successor relations, and any reviewed override. `replay_evidence_freshness_assessment` verifies the canonical calculation hash.

Dependencies can reference claims, assessments, searches, answers, briefs, and watches. Propagation emits an immutable impact state:

- `current` when every support remains current;
- `mixed-age` when current and non-current evidence coexist;
- `unsupported-currently` when the last current support is lost.

The impact includes a decay-derived ranking factor. Identical assessment sets deduplicate to the same impact ID, while a later recovery creates an explicit `freshness-recovered` transition.

## Safe inspection and simulation

Read-scoped MCP tools expose policies and stored explanations, find evidence expiring within a bounded horizon, simulate rule overrides, and compare immutable policy versions. Simulation and comparison do not write assessments, impacts, or audit rows. Scan limits are clamped to 1,000 items and long-running entry points accept cancellation flags.

Authorization is separated into `knowledge:freshness:read`, `knowledge:freshness:write`, and `knowledge:freshness:review`. Namespace filters are applied to every evidence, assessment, dependency, and impact query. Writes are atomic, idempotent, and audited; reviewer overrides require explicit supporting evidence and may expire.

The offline conformance suite exercises the six primary knowledge-engine domains: research, political, economic, OSINT, technical, and scientific.
