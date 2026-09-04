# Research-gap discovery

Noesis records what it does not know as first-class, versioned knowledge. A research gap references an existing claim, entity, event, time range, geography, or methodology identity and a precise coverage dimension. It never substitutes a new object store for those identities.

## Coverage and policy

Coverage observations preserve support evidence IDs, source identities and classes, independence groups, accessibility, primary-source status, currency, methodological adequacy, stance, citation chains, generation, valid/observed time, producer, policy context, and provenance. `coverage_known: false` produces an explicit `unknown-coverage` gap rather than guessing that support is absent.

Versioned policies set minimum primary, independent, current, and methodologically adequate support. Upgrades supersede but do not rewrite prior policies. Detection can therefore be replayed or run against a pinned historical version.

The detector reports overlapping shortfalls separately. It also identifies inaccessible or retracted support, meaningful contradiction clusters, missing originals, and circular citation chains. Mirrored or content-identical reports are not treated as independent contradictions.

## Stable lifecycle

A gap ID is stable for its namespace, object identity, dimension, and gap type. Changes create append-only revisions with `open`, `in-progress`, `resolved`, or `dismissed` status. Repeated scans are idempotent; improved coverage automatically resolves conditions that are no longer present, while reviewed status changes require a reason and supporting evidence.

Gap reports support deterministic cursor pagination, type/status/object filters, explanation drill-down, and before/after coverage comparisons. Calculation hashes make current revisions replayable.

## Bounded research tasks

The planner ranks open gaps using policy weights for decision relevance, uncertainty reduction, feasibility, freshness, user priority, and cost. It applies a hard total budget, a maximum task count, and blocked source classes. Ties resolve by stable gap ID. Each proposed task contains an executable action, query, source class, coverage constraints, and evidence requirement; identical plans deduplicate to the same task IDs.

MCP authorization is split into `knowledge:gaps:read`, `knowledge:gaps:write`, and `knowledge:gaps:review`. Discovery is capped at 1,000 coverage records and supports cancellation; list pages and task plans are capped at 500. The offline conformance suite covers research, political, economic, OSINT, technical, and scientific namespaces.
