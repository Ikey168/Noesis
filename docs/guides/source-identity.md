# Source identity and ownership graph

Noesis assigns stable namespace-scoped identities to publications,
organizations, agencies, authors, channels, accounts, and unknown sources while
retaining every source-native identifier and multilingual name. Renames and
deleted accounts append immutable revisions; duplicate display names do not
force a merge.

Reviewed alias decisions normalize URLs, domains, handles, identifiers, and
names. Shared domains and rebrands may deliberately resolve to multiple
candidates. A split decision reverses a link without deleting the earlier
decision, preserving reviewer, reason, confidence, and citation provenance.

Ownership, funding, editorial control, state affiliation, syndication,
authorship, and reporting origin are sourced relationship revisions with valid
and observation times, confidence, uncertainty, producer, and policy context.
Conflicting disclosures can coexist. As-of dossiers and bounded paths select
only relationships valid at the requested time and report whether every edge
has citations.

The independence explainer groups sources connected through ownership,
editorial control, syndication, shared authorship, or reporting origin. It
returns the exact relationship revisions used. Anonymous or missing identities
make the result explicitly incomplete; absence of a graph edge is never treated
as proof that two reports are independent.

MCP operations provide registration, revision/deletion, alias link/split and
resolution, sourced relationship writes/retractions, identity lookup/history,
opaque-cursor dossiers, as-of paths, and independence explanations. Access is
split across `knowledge:source-identity:read`,
`knowledge:source-identity:write`, and
`knowledge:source-identity:review` scopes.
