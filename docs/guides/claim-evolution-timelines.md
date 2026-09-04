# Claim evolution timelines

Claim timelines preserve the existing `argument_claims.claim_id` as the
canonical claim identity. The subsystem adds immutable semantic states and
sourced evolution edges; it does not copy or replace the claim corpus.

A claim state records wording, stance, certainty, epistemic status,
attribution and quotation, scope, alternative interpretations, source and
document revision, evidence, generation, valid time, observation time,
producer, policy, and provenance. Quantity mentions retain their reported unit
and are normalized through the quantitative unit registry, allowing a semantic
diff to distinguish a changed number from an equivalent unit conversion.
Retracted source revisions remain visible and are labelled accordingly.

Many-to-many edges model successors, refinements, reversals, withdrawals, and
branches independently from document revisions. Each edge requires evidence,
confidence, a method, and a match explanation. Cycles are rejected. Evidence
from retracted sources is retained but marked `retracted-only`, so lineage is
historically complete without treating withdrawn support as current.

Offline successor detection uses canonicalized lexical overlap plus explicit
numeric, negation, scope, and hedging signals. Optional embedding scores are
accepted only with a pinned model name, version, and revision. Candidates are
bounded, deterministically ranked, and retain their explanations; false
matches below the requested threshold are omitted. Detection supports
cancellation and never persists links implicitly.

Timeline reads traverse branches with bounded depth and result size. They pin
observation time and generation, sort deterministically, and use cursors bound
to the query filters. Semantic diffs expose material wording, stance,
certainty, epistemic, attribution, quantity, scope, interpretation, and source
retraction changes together with both citation sets. Source comparisons and
component replay preserve citation closure.

MCP access uses `knowledge:claim-timeline:read` and
`knowledge:claim-timeline:write`. The tools cover state capture, reviewed
lineage links, successor detection, state lookup, timeline, diff,
source comparison, and replay.
