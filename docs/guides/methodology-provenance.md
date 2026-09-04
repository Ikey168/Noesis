# Methodology provenance

Noesis models a study's design as versioned knowledge. A stable study identity
can have append-only revisions containing its design, population, samples,
instruments, interventions, comparators, outcomes, datasets, and analysis plans.
Every revision retains generation, valid and observed time, producer, policy,
and source provenance. Experimental and observational designs use the same
contract; missing fields remain empty or unknown rather than being inferred.

Method statements extracted from papers, reports, datasets, or supplements
must carry a document identity and a page, section, table, or passage locator.
Confidence and extraction uncertainty remain attached to each statement.
Different descriptions of the same method kind are returned as a conflict
group, including disagreements between a main paper and its supplement.
Extraction is capped at 500 statements, supports cancellation before commit,
and creates a deterministic replay receipt.

Bias and applicability assessments are sourced or reviewed append-only
revisions. Multiple frameworks can coexist, reviewer disagreement is retained,
and `rating: null` explicitly means that the rating is unknown. Evidence
statement references are checked so an assessment cannot cite a missing method
statement.

Study artifact links cover preregistrations, protocols, datasets, code,
replications, comments, errata, and retractions. They retain artifact version,
availability, locators, identifier mismatches, and indirect-replication paths.
Comparison and evidence-strength tools explain their inputs and never turn an
unknown assessment into a positive or negative rating.

MCP access is split across `knowledge:methodology:read`,
`knowledge:methodology:write`, `knowledge:methodology:extract`, and
`knowledge:methodology:review`. All reads are namespace-isolated and bounded;
writes are idempotent, atomic, and audited. Offline fixtures cover scientific
trials, observational studies, and social-science surveys.
