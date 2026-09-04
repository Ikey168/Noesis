# Source acquisition planner

The Source Acquisition Planner turns a research objective into an explicit, budgeted sequence of queries against Noesis source packs. Selection is based on declared coverage and constraints, never an opaque model preference.

## Credential-safe capability registry

Each immutable source capability version references an existing source identity and connector. It describes domains, evidence classes, authority basis, access and licensing conditions, latency, cost, rate limits, query forms, and an ownership or dependency group. Credential reference names may be stored; credential values, tokens, passwords, and authorization headers are rejected recursively and never enter plans, receipts, or audit rows.

New versions must explicitly supersede the current version. Plans pin exact capability IDs, so execution detects registry drift instead of silently changing sources. Availability checks distinguish missing credentials, unaccepted or incompatible licenses, outages, and capabilities that are too old for the objective.

## Objectives and explainable plans

An objective preserves the question decomposition, desired evidence classes, minimum independent groups, freshness bound, budget, result/page/time/retry bounds, redistribution policy, required/forbidden sources, and allowed licenses. Defaults are canonical, and conflicting constraints fail before a plan is created.

Preview is read-only. It reports selected steps, adaptive fallbacks, exclusions, score components, evidence coverage, independent groups, projected cost, and any infeasibility. Sources are ordered by explicit coverage, authority, latency, cost, and registry freshness; deterministic source IDs break ties. Redundant sources from the same dependency group are excluded from the main path but may remain pinned fallbacks.

## Execution and recovery

Authorized execution translates each step into a bounded request for its pinned source-pack connector. Source-pack preflight remains responsible for endpoint, network, credential, license, circuit, mapping, and ingestion safety. Planner checkpoints add cross-source step state, attempts, cursors, actual cost, and receipt hashes.

Transient rate limits, timeouts, and unavailability retry only within the objective bound. A failed primary can activate a pinned fallback. Completed checkpoints are reused after a process crash, actual spending cannot exceed the plan budget, and cooperative cancellation stops before the next step. A repeated terminal execution key returns the same receipt; replay verifies its canonical hash.

MCP authorization uses `knowledge:source-planner:read`, `knowledge:source-planner:write`, and `knowledge:source-planner:execute`. Live networking is opt-in; otherwise execution uses installed source-pack fixtures. The conformance suite evaluates research, political, economic, OSINT, technical, and scientific namespaces.
