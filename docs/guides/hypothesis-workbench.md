# Hypothesis Workbench

The Hypothesis Workbench keeps competing explanations explicit while research
is still uncertain. A namespace-scoped workspace contains hypotheses with
stable identities, assumptions, discriminating predictions, and alternative
links. Draft, active, retired, and branched states are immutable revisions that
retain generation, valid time, observation time, producer, policy, and actor
context.

Evidence links record support, contradiction, or ambiguity; relevance; exact
source revision; provenance; reviewer annotations; and a declared independence
group. Duplicate copies in the same group count once. Retractions append a new
link revision. Source-scoped evidence is omitted for unauthorized readers and
the comparison/export reports the omission without leaking its contents.

`compare_hypotheses` offers qualitative or rounded weighted summaries. It can
make priors and sensitivity explicit, widens intervals when evidence is sparse,
preserves ties, and always states that its scores are comparison aids—not truth
probabilities.

Research plans are derived from discriminating predictions and limited to 100
steps. Execution consumes a caller-supplied budget and observation receipts,
persists a cursor, and records unresolved source or budget gaps. Paused or
cancelled runs can resume without repeating completed steps.

The MCP surface supports workspace creation, inspection, optimistic revision,
branching, retirement, evidence linking/retraction, comparison, planning,
execution, export, and replay. Writes require `knowledge:hypothesis:write`,
execution requires `knowledge:hypothesis:execute`, and reads require
`knowledge:hypothesis:read`. Exports include every accessible workspace and
evidence revision and carry a deterministic hash that replay verifies.
