# Market watchlists and alerts

Market alerts are immutable, owner-scoped watch revisions evaluated against a
bounded observation snapshot. A watch may contain price or metric thresholds,
new filings, economic releases, source corrections, and thesis-review
triggers. Each run records the rule version, cutoffs, observed value, source
revision IDs, input hash, and every suppressed or triggered decision.

The REST and MCP adapters expose the same capability service:

- save a watch with `save_market_alert_watch` or `POST /api/v1/market/alerts/watches`;
- evaluate observations with `run_market_alerts` or `POST /api/v1/market/alerts/run`;
- inspect the persisted watch/run and use `market_alert_history` for delivery history;
- deliver through `deliver_market_alert` or `POST /api/v1/market/alerts/deliver`.

Delivery delegates to the shared Knowledge Anomaly store. Its cooldown/dedup
window, quiet-until, retry delay, cancellation, acknowledgement/resolution
history, and health machinery are therefore shared with non-market watches.
Source references are checked against the current market entitlement policy at
run and delivery time. Revoked or missing rights suppress the notification and
never return source payloads.

Observations must be dated and runs must provide public and acquisition
cutoffs. A watch can declare `stale_after_ms`; observations older than that
window are recorded as suppressed rather than evaluated. Rule revisions are
immutable: change the version to change a rule. Local fixture tests cover
replay/idempotency, owner isolation, stale data, changed rules, entitlement
revocation, deduplication, retry, and delivery history. Live provider
coverage, notification transport credentials, and human workflow acceptance
remain deployment concerns.
