# noesis-claim-watch-v1 — durable evidence-change subscriptions

Claim Watches turn a saved selector into an ongoing, auditable trust loop.
The public operation remains additive to `noesis-kb-v1`; watch records,
polls, and events identify their nested contract as
`noesis-claim-watch-v1`.

The governed schema is
`contracts/schemas/jsonschema/noesis-claim-watch-v1.json`.

## Selectors and events

A selector contains exactly one `type` and `value`. Supported types are
`query`, `claim`, `entity`, and `topic`. A watch subscribes to one or more of:

- `support_gained` / `support_lost`;
- `contradiction_added` / `contradiction_removed`;
- `independence_changed`;
- `integrity_changed`;
- `quantitative_verdict_changed`;
- `coverage_stale`;
- `source_delivery_failed`;
- `guidance_stale` (private watch only, when authorized guidance conflicts
  with a newer public record).

Every event is immutable and includes its reason code, explanation, complete
before/after snapshots, committed consolidation watermark, observation time,
and at least one evidence locator. Supporting publication counts and distinct
source identities remain separate; watches do not manufacture a trust score.

## Watermarks, idempotency, and cursors

The consolidation pipeline first calls `commit_watch_watermark`. The matcher
refuses unknown watermarks, so it cannot persist progress from a half-finished
consolidation run. Snapshot, event, and progress writes are atomic per watch.
A deterministic idempotency key prevents duplicate logical events on retry.
Failures leave progress unchanged and become dead-lettered after three failed
attempts.

Poll cursors begin with `cw1.` but are otherwise opaque to clients. They are
bound to one watch, carry the last consumed immutable event sequence, and are
strictly ordered. Polling is exclusive of the cursor. An empty event list is a
successful result and returns a resumable cursor. V1 retains events and
snapshots indefinitely; a future retention policy must advance
`retained_from_sequence`, after which older cursors return `cursor_stale`.

Confirmed deletion is a soft delete: the watch stops matching and disappears
from normal reads, while immutable events and snapshots remain available for
audit/replay. Pause and resume are idempotent.

## Authorization and observability

Every watch is bound to an authenticated `principal_id` and one domain.
Owner checks apply to every lifecycle operation, poll, and replay. Domains
tagged `private` additionally require an explicit `grant_watch_domain` entry;
public-domain snapshots can only read through their resolved domain backing,
so they cannot acquire private locators.

Lifecycle changes and event reads create audit entries containing identifiers,
counts, and state labels only. Operational metrics expose matcher lag, event
volume, unresolved failures, and dead-letter counts; selectors, source text,
URLs, and secret values are excluded.

## Surfaces and restart-safe polling

Direct Python:

```python
from src.kb.contract import watch_create, watch_poll

created = watch_create(
    "economics",
    "principal-123",
    {"type": "topic", "value": "inflation"},
    ["support_gained", "contradiction_added"],
)
page = watch_poll(created["data"]["watch_id"], "principal-123")
cursor = page["data"]["cursor"]  # persist this outside the process
```

MCP:

```json
{
  "name": "watch_poll",
  "arguments": {
    "watch_id": "watch:0123456789abcdef01234567",
    "principal_id": "principal-123",
    "cursor": "<cursor persisted from the prior poll>",
    "limit": 50
  }
}
```

The MCP host must bind `principal_id` from its authenticated session before
invoking these tools; it must not accept an unverified end-user identity.

REST (the JWT subject becomes the principal; caller-supplied identity headers
are not trusted):

```console
curl -H 'Authorization: Bearer <access-token>' \
  'http://localhost:8000/api/v1/kb/watches/watch:0123456789abcdef01234567/events?cursor=<persisted-cursor>&limit=50'
```

The DuckDB repository stores watches, snapshots, events, and cursors. After a
process restart, reconnect to the same database and pass the persisted cursor;
the next poll returns only events with a greater sequence.

## Replay and brief consumption

`scripts/replay_claim_watches.py` recomputes logical transitions from retained
watermark snapshots and compares their idempotency keys with stored events.
`src.kb.brief.watch_event_digest` can render polled events into a bounded brief
section. It consumes the immutable event repository and never becomes the
event source of truth.
