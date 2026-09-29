# Optional Jev workflow advice

Noesis offers four additional hosted suggestion paths. Each requires an explicit
`network_approved` flag, a bounded policy and budget, and the trusted
`TYPESAFE_API_KEY` environment variable. They produce advice; the existing
workflow remains the authority for routing, review, revision history, and source
acquisition. Current tests use fixtures. No human-labeled Jev acceptance result
has been established for these tasks.

## Intake route and review priority

`register_jev_intake_intent` retains a bounded owner-scoped free-text intent.
`suggest_jev_intake_route` binds its current version, asks ten mode questions,
then passes answered booleans through the deterministic mode router. Explicit
answers or an explicit override route directly. Missing or abstained answers
return pending; revising the intent invalidates the old suggestion.

`suggest_jev_review_priority` checks the current review target, votes, and exact
source revisions before estimating impact. Its suggested priority combines
machine impact and observed reviewer disagreement, separately from stored
priority. `inspect_jev_review_priority` detects changed targets, sources, or
votes. Neither tool creates a vote or resolution. The fixed-budget evaluator in
`src/evaluation/jev_review_priority.py` compares high-impact errors found under
the same reviewer budget.

## Revision significance

`suggest_jev_revision_significance` accepts adjacent retained before/after
document revision IDs. It sends bounded changed passages with both exact
revision bindings and reports covered offsets. An oversized changed passage is
pending even if Jev answers; unchanged text produces no provider request. The
stored revision's change class and lifecycle remain authoritative. Advice does
not rewrite captured text, revision identity, correction/retraction status, or
change briefs. `src/evaluation/jev_revision_significance.py` reports
substantive-change recall, cosmetic false-positive rate, and answer coverage
against independent labels or explicit fixtures.

## Eligible source relevance

`suggest_jev_source_relevance` first runs the existing source planner's domain,
query-form, credential, license, availability, freshness, and budget filters.
Only budget-feasible selected and fallback capabilities are sent to Jev. The
request binds the objective hash and each current capability ID/hash. A score
can blend into an **unpersisted preview** with transparent baseline, semantic
relevance, and 0.2 weight; the baseline plan is returned alongside it. Hard
filters, required sources, cost arithmetic, and execution authorization still
belong to `SourcePlannerStore`. No source plan is persisted by this tool.
`src/evaluation/jev_source_planning.py` compares evidence yield and objective
coverage at an equal acquisition budget. Independently labeled evaluation is
required before accepting semantic scores into an acquisition plan.
