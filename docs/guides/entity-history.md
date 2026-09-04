# Entity merge-and-split history

Entity identity changes are immutable reviewed decisions. Alias, match,
non-match, merge, split, redirect, review, and undo events keep stable event
identities, revisions, evidence provenance, generation, valid/observed time,
producer, and policy context. Conflicting reviewers remain visible.

Merge previews resolve redirect chains, reject cycles and cross-namespace
references, show affected dependencies, and enforce dual control when enabled.
Execution atomically publishes redirects while preserving every original entity.
Undo appends a new event and deactivates the projection; it never deletes
history.

Split previews create stable identities and assign mentions, claims, relations,
or events only when a reviewed evidence-level decision exists. Partial splits
retain ambiguous objects explicitly. New aliases and assignments are committed
atomically and can be rolled back through the same ledger.

Graph, search, summary, watch, bundle, and metric dependencies can be marked
affected or independent. Rebuild failures publish no generation; a successful
bounded set is published atomically. Snapshot-bounded resolution and paginated
history remain available throughout.

MCP scopes separate read, write, review, and execute authority under
`knowledge:entity-history:*`. Audit exports and six-domain fixtures verify
namespace isolation and decision closure.
