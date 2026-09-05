# Systematic reviews

Register a protocol using `create_review_protocol` with a question, inclusion and
exclusion criteria, databases, search expressions, ISO date range, at least two
distinct reviewer IDs, and planned study fields. Amendments require the current
revision and a rationale. Candidate publications retain their original protocol
revision; amendments never silently rewrite completed screening.

`add_review_candidate` records publication identity, source revision/namespace,
search-run ID, study ID, title/abstract, and full-text availability. Different
publications can share a study ID. Repeated searches and source revisions remain
separate traceable candidates. Acquisition receipts and availability are supplied
provenance; the export discloses that they have not been independently verified.

Registered reviewers use `list_review_candidates`, `inspect_review_candidate`,
and `screen_review_candidate`. Other reviewers' decisions remain hidden on these
surfaces. Both title/abstract and full-text stages require include/exclude/pending
with a reason and the reviewer's expected revision. Full-text screening requires
resolved title/abstract inclusion. Unavailable full text cannot be marked included
or excluded. A newly acquired version can be added as a new candidate preserving
the old unavailable observation.

The coordinator can inspect disagreements and call `adjudicate_review_candidate`
with the current screening hash. Later reviewer changes invalidate prior
adjudication matches. Current protocol participation and namespace scopes are
checked on every operation; removing a reviewer prevents historical access too.

## Evidence table

`extract_review_field` proposes a protocol-defined value with an exact character
span in the candidate's committed document revision. The returned quote is read
from that source, not supplied by the proposer. Missing source text or an invalid
span fails explicitly. Reading shared-corpus text requires explicit
`document:<publication_id>:read` (or operator) in addition to namespace access;
a caller-supplied namespace reference cannot grant access to an arbitrary source.
Another registered reviewer uses `review_study_field` to
accept or reject it; conflicting field reviews remain disputed. Recorded values
are interpreted fields with visible support and review status, not automatic
claims of correctness. Page-only extraction and automatic model extraction are
not supplied by this path.

`export_systematic_review` returns candidates, study fields, screening reasons,
unresolved states, protocol amendments, source versions, and separate candidate,
publication, and study counts. Counts reconcile with the exported ledger. The
export has an explicit size budget and fails rather than silently dropping rows.

## Optional tools and reporting

[ASReview's documented tabular format](https://asreview.readthedocs.io/en/stable/lab/data_format.html)
supports title/abstract input. Noesis exports UTF-8-compatible CSV with stable
candidate IDs and no inclusion labels for optional screening prioritization.
ASReview is not a runtime dependency and its ranking cannot write screening
decisions. This evaluates interoperability, not a recall or safe-stopping claim;
that evaluation requires an independently screened corpus.

The export maps available records to [PRISMA 2020 reporting items](https://www.prisma-statement.org/prisma-2020-checklist):
question/criteria, search plan, reviewers, field collection, selection counts,
exclusion reasons, study characteristics, and protocol amendments. Actual search
dates still require source receipts. Risk-of-bias assessment, certainty, synthesis,
and other reporting requirements remain separate. A ledger or flow diagram does
not establish methodological compliance.
