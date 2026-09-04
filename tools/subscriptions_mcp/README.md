# Knowledge Subscriptions MCP

Saved deterministic queries run only at committed ingestion or consolidation
watermarks. Events are replayable with opaque cursors and retain before/after
evidence. Polling is the default; webhook, email, and queue values create
channel-neutral outbox records for a separate authorized delivery worker.

Reads require `knowledge:subscriptions:read`, lifecycle writes require
`knowledge:subscriptions:write`, and outbox delivery requires
`knowledge:subscriptions:deliver`, plus a namespace read grant.
