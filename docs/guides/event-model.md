# Event-centric knowledge model

The event model extends Noesis's existing canonical-event resolver. The legacy
`canonical_events` table remains the compatibility projection, while immutable
event revisions preserve stable identity, type, lifecycle, granularity,
recurrence, generation, valid time, observation time, producer, policy, and
provenance. Corrections and cancellation never replace prior states.

Bounded mention ingestion clusters multilingual source mentions using
deterministic type, participant, place, temporal, and recurrence features. An
optional classifier is accepted only with a pinned name, version, and revision;
the deterministic path remains available offline. Every mention retains its
original text, exact document revision, features, confidence, and alternatives.

Participants and roles, disputed locations, uncertain time ranges, quantities,
causes, and consequences are separate sourced accounts. Incompatible accounts
coexist with confidence and uncertainty rather than being collapsed into one
narrative. Quantity accounts retain the reported unit and add a normalized
value. Late evidence appends an account revision, and reviewed retractions keep
the earlier account history.

Sourced predecessor, successor, recurrence, cause, and consequence edges drive
bounded neighborhoods, including cross-domain links. Search uses opaque cursors
bound to its filters and optional snapshot generation. Timeline, observation
as-of lookup, exact revision reads, semantic diff, and predecessor-chain replay
make evolving events reproducible.

MCP access is separated into `knowledge:event:read`, `knowledge:event:write`,
and `knowledge:event:review`. The new tools coexist with the earlier event
resolution and reversible merge operations for compatibility.
