---
name: awareness-inbox
description: Run the Noesis Awareness intake mode end to end — subscribe RSS/Atom/newsletter feeds, refresh the inbox, triage items against a bounded Awareness session, and promote escalated items into another intake mode (Deep Research, Decision Support, …). Use when the user wants to stay current on a stream, process a feed inbox, or route a noteworthy item into deeper work. Drives the noesis-knowledge-engine MCP feed/awareness tools.
---

# Awareness inbox (native workflow)

Awareness is the one intake mode with a full **native workflow**: a durable feed
inbox feeding time-boxed Awareness sessions. Tools live on the **`noesis-knowledge-engine`**
MCP server (`mcp__noesis-knowledge-engine__*`). Awareness has a tight budget (**15 min**,
max 15) — it is triage, not deep work; promote anything that needs more.

## 1. Subscribe feeds

`subscribe_intake_feed(namespace, url, name, source_kind?)`
- `url` must be **public, credential-free HTTPS** RSS/Atom (or a `newsletter_feed`
  that explicitly offers RSS/Atom — mailbox ingestion is not implemented).
- A subscription is owned by one caller + namespace.
- Manage with `list_intake_feed_subscriptions(namespace)`.

## 2. Refresh + read the inbox

- `refresh_intake_feed_inbox(namespace)` — needs `knowledge:intake:fetch`; fetches
  at most 50 feeds × 100 entries via the bounded RSS/Atom transport (no implicit
  redirects or proxy creds).
- `list_intake_feed_inbox(namespace, …)` / `inspect_intake_feed_item(namespace,
  item_id)` — browse and open items.
- `mark_intake_feed_read(...)`, `annotate_intake_feed_item(...)`,
  `decide_intake_feed_item(...)` — lightweight per-item actions.

### Signal rules (optional pre-filter)
`preview_intake_feed_signals`, `save_intake_feed_signal_rule`,
`list_intake_feed_signal_rules`, `preview_intake_feed_signal_rule` — define/test
rules that flag matching items so triage focuses on what matters.

## 3. Triage in a bounded Awareness session

1. `start_awareness_from_inbox(namespace, request_key, intent?, duration_minutes?)`
   — opens a bounded queue from **unprocessed** inbox items. `request_key` is
   stable (safe replay).
2. For each item: `triage_awareness_item(namespace, session_id, item_id,
   command_key, expected_revision, decision)` — atomically records the decision in
   **both** the inbox and the session. `decision` ∈
   `watch | escalate | schedule | discard | archive | flag`.
   - `expected_revision` must match the session's current revision; `command_key`
     is stable per item (replay-safe).
   - `triage_awareness_batch(...)` applies several decisions at once.

## 4. Promote what deserves more

`promote_awareness_item(namespace, awareness_session_id, item_id, request_key,
target_mode, reason, intent, workspace_links?)` — turns an **escalated** item into
a new session in a deeper mode (e.g. `target_mode="Deep Research"` or
`"Decision Support"`), carrying the item's source + annotation references so the
new session starts with provenance intact.

## Typical loop

```
subscribe_intake_feed(namespace="daily", url="https://example.org/feed.xml", name="Example")
refresh_intake_feed_inbox(namespace="daily")
s = start_awareness_from_inbox(namespace="daily", request_key="aw-2026-09-22")
# for each surfaced item:
triage_awareness_item(namespace="daily", session_id=s.session_id, item_id="...",
                      command_key="aw-item-1", expected_revision=0, decision="escalate")
promote_awareness_item(namespace="daily", awareness_session_id=s.session_id, item_id="...",
                       request_key="promote-1", target_mode="Deep Research",
                       reason="worth a systematic look", intent="delta flood modelling")
```

The generic session lifecycle (route/start/command/export for any mode) is in the
**intake-mode** skill.
