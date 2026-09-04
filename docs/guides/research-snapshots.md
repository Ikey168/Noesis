# Snapshot-pinned research sessions

Research sessions keep a sequence of MCP calls on one reproducible knowledge
state even while ingestion and maintenance continue. A session pins a
generation vector for requested source packs, namespaces, derived projections,
artifact watermarks, active schemas, and snapshot-capable federated sources.

Begin a session with `begin_research_snapshot`, retaining the returned bearer
token securely. The token is shown only once; persisted session records contain
only its SHA-256 digest. `inspect_research_snapshot` returns the selection and
generation vector without returning the token. Sessions are principal-bound,
scope-bound, time-limited, renewable only up to their maximum lifetime, and can
be closed explicitly.

Pass the token as `snapshot_token` to `explain_knowledge_query` and
`query_knowledge`. Noesis binds the session ID and vector hash into the
normalized request hash. Query plans, replay records, and opaque pagination
cursors therefore cannot be reused across research sessions. A query also
cannot expand beyond the domains and namespaces selected at session creation.

Federated providers must declare `snapshot` or `backend-snapshot` consistency
and an immutable generation or revision. By default, an unavailable local or
remote generation prevents session creation. `allow_degraded` records explicit
omissions instead; it never silently substitutes a current read.

`research_snapshot_pins` lists records retention and compaction must preserve
until closure or expiry. `research_snapshot_health` reports session and pin
counts without mutating the warehouse.
