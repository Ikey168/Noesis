# Subscription events and delivery

The existing `removed` event type is retained for compatibility. It means an object left the saved query's result set. Consumers should inspect `evidence.removal` before interpreting that event as evidence withdrawal.

| Reason | Meaning |
|---|---|
| `result-set-absence` | The item is absent; the cause is unknown. |
| `incomplete-coverage` | Missing or unavailable coverage prevents a withdrawal conclusion. |
| `filter-changed` | The saved query's filters changed since its prior snapshot. |
| `possible-top-k-displacement` | Ranking or truncation may have displaced the item. |
| `confirmed-deleted`, `confirmed-retracted` | The evaluator supplied an explicit deletion/retraction and source revision ID in `coverage.removals`. |

Only the last two reasons set `withdrawal_confirmed: true`. Poll events and outbox payloads carry the same coverage and removal evidence. Historical events retain their original payloads; absence of removal metadata must not be interpreted as confirmation.

## Outbox lifecycle

The subscriptions MCP exposes claim, acknowledge, fail, and redrive operations. Each requires `knowledge:subscriptions:deliver`, current namespace read access, and current subscription ownership. Claim returns an opaque lease token. Acknowledgement or failure must present the current unexpired token; another worker can reclaim an expired lease. Receiver failures receive exponential backoff, capped at one hour, and become terminal after the configured attempt limit (default five). Redrive requires a stable request key and only resets terminal failures.

Delivery is at least once. A crash after the receiver accepts an event and before acknowledgement can cause another send. The stable `event_id` is the receiver's idempotency key; lease tokens change on each attempt and are unsuitable for receiver deduplication. Acknowledgement replay is idempotent while its lease token remains the recorded outcome.

`src.kb.subscription_delivery.deliver_once` accepts explicitly configured transports keyed by `(delivery_kind, destination_ref)`. Tests inject a fake receiver. Deployments can bind a webhook separately:

```python
from src.kb.subscription_delivery import SubscriptionDeliveryStore, deliver_once, webhook_transport

transports = {("webhook", "research-alerts"): webhook_transport(configured_url, timeout=10)}
deliver_once(SubscriptionDeliveryStore(conn), "worker-1", transports,
             principal_id=principal, scopes=current_scopes, limit=20)
```

The HTTP transport uses a bounded timeout, rejects redirects, and sends `Idempotency-Key`. URLs come from deployment configuration, not event payloads. No external destination is enabled by default. Polling remains available without a transport.
