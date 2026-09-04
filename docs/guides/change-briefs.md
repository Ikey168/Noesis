# Semantic change briefs

Noesis normalizes changes to claims, entities, events, metrics, policies,
methods, and evidence coverage as stable before/after events. Classification
distinguishes additions, removals, corrections, reclassifications, numeric
changes, retractions, and cosmetic edits. Ranking uses a pinned policy over
magnitude, novelty, decision relevance, source authority, and user priority;
ties remain deterministic.

Every generated brief carries exact before/after evidence, generations,
materiality score, policy revision, uncertainty, and a replay hash. Missing
prior state, conflicting sources, or incomplete coverage is disclosed. The
generator does not synthesize facts beyond the supplied states and citations.

Subscriber-scoped windows aggregate and deduplicate bursts. Delivery retries
reuse the same identity, quiet windows are explicit, cancellation commits
nothing, and acknowledgments are durable. Feedback requires a separate review
scope. MCP permissions are split into `knowledge:briefs:read`,
`knowledge:briefs:write`, `knowledge:briefs:deliver`, and
`knowledge:briefs:review`; history is bounded and namespace-isolated.
