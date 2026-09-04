# Epistemic status engine

Noesis keeps two questions separate: what kind of statement is this, and what
does the available evidence currently justify? The versioned core taxonomy
distinguishes facts, attributed reports, allegations, estimates, forecasts,
opinions, hypotheses, normative claims, and unknowns. Domain taxonomies may add
categories without removing or redefining the core vocabulary inside an
existing semantic version.
Use `register_epistemic_taxonomy` to add an immutable taxonomy and
`list_epistemic_taxonomies` to inspect the registered core and domain versions.

`classify_epistemic_statement` uses deterministic, inspectable rules offline.
The Python engine can also accept a classifier, but only with a pinned name,
version, and revision; it records the model result, features, confidence, and
the deterministic fallback. Classification is never presented as proof that a
statement is true.

`assess_epistemic_statement` groups evidence by independence identity so copied
wire reports do not count as separate confirmation. It combines stance,
reliability, freshness, and methodology into `supported`, `contested`, or
`insufficient`, retaining the full factor receipt and explicit uncertainty.
Each atomic revision preserves its namespace, source revision, generation,
valid time, observation time, producer, policy, and writing principal. Repeated
identical input is idempotent.

Authorized reviewers use `review_epistemic_status` with a substantive reason.
The optional `expected_assessment_id` provides optimistic concurrency control.
Overrides change only the effective status: the machine result remains intact,
and every reversal is linked in an append-only transition history.

Use `get_epistemic_assessment` for the current revision or full history,
`search_epistemic_assessments` for bounded status/state filtering and facets,
and `explain_epistemic_assessment` for a stable account of classification,
evidence factors, policy, provenance, review history, and limitations. MCP
access is separated into `knowledge:epistemic:read`,
`knowledge:epistemic:write`, and `knowledge:epistemic:review` scopes.
