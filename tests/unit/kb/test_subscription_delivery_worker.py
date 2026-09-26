import time

import duckdb
import pytest

from src.kb.subscription_delivery_worker import SubscriptionDeliveryWorker
from src.kb.subscriptions import SubscriptionError, SubscriptionStore


SCOPES = {"knowledge:subscriptions:read", "knowledge:subscriptions:write",
          "knowledge:subscriptions:deliver", "namespace:research:read"}
AUTH = {"principal_id": "alice", "scopes": SCOPES}


def config(ref="approved"):
    return {"enabled": True, "principal_id": "alice", "scopes": sorted(SCOPES),
            "max_per_tick": 5,
            "destinations": [{"kind": "webhook", "ref": ref,
                              "url_env": "NOESIS_APPROVED_WEBHOOK_URL"}]}


def create_event(store, ref, key):
    subscription = store.create({
        "namespace": "research", "query": {"operation": "search", "text": key},
        "delivery": {"kind": "webhook", "destination_ref": ref}}, key, **AUTH)
    store.evaluate(subscription["subscription_id"], 1,
                   {"items": [{"id": key}], "coverage": {"complete": True}}, **AUTH)
    return subscription["subscription_id"]


def test_unattended_worker_only_claims_explicit_destinations_and_restarts(tmp_path):
    path = str(tmp_path / "subscription-delivery.duckdb")
    conn = duckdb.connect(path)
    store = SubscriptionStore(conn)
    store.commit_watermark("research", 1)
    create_event(store, "unconfigured", "first")
    create_event(store, "approved", "second")
    received = []
    transport = lambda payload, *, idempotency_key: received.append((payload["event_id"], idempotency_key))
    worker = SubscriptionDeliveryWorker(
        conn, config(), transports={("webhook", "approved"): transport})
    assert worker.readiness()["ready"]
    tick = worker.tick("maintenance-1")
    assert tick["attempted"] == tick["delivered"] == 1
    assert received[0][0] == received[0][1]
    assert worker.tick("maintenance-1")["attempted"] == 0
    conn.close()

    conn = duckdb.connect(path)
    worker = SubscriptionDeliveryWorker(
        conn, config(), transports={("webhook", "approved"): transport})
    assert worker.tick("maintenance-2")["attempted"] == 0
    outstanding = worker.store.pending(**AUTH)
    assert len(outstanding) == 1 and outstanding[0]["destination_ref"] == "unconfigured"
    conn.close()


def test_worker_opt_in_auth_and_bounded_retry():
    conn = duckdb.connect(":memory:")
    store = SubscriptionStore(conn)
    store.commit_watermark("research", 1)
    create_event(store, "approved", "third")
    with pytest.raises(SubscriptionError) as disabled:
        SubscriptionDeliveryWorker(conn, {**config(), "enabled": False}, transports={})
    assert disabled.value.code == "delivery_disabled"
    with pytest.raises(SubscriptionError) as unavailable:
        SubscriptionDeliveryWorker(conn, config(), environ={})
    assert unavailable.value.code == "destination_unavailable"
    calls = []

    def flaky(payload, *, idempotency_key):
        calls.append(idempotency_key)
        if len(calls) == 1:
            raise RuntimeError("temporary receiver failure")

    clock = [int(time.time() * 1000) + 100]
    worker = SubscriptionDeliveryWorker(
        conn, config(), transports={("webhook", "approved"): flaky}, now=lambda: clock[0])
    failed = worker.tick("maintenance")
    assert failed["attempted"] == failed["retrying"] == 1
    clock[0] = failed["receipts"][0]["available_at_ms"]
    accepted = worker.tick("maintenance")
    assert accepted["delivered"] == 1 and len(set(calls)) == 1
    conn.close()


def test_enabled_worker_initializes_empty_warehouse_for_readiness():
    conn = duckdb.connect(":memory:")
    worker = SubscriptionDeliveryWorker(
        conn, config(), transports={("webhook", "approved"): lambda *_args, **_kwargs: None}
    )
    assert worker.readiness()["ready"]
    assert worker.tick("maintenance") == {
        "contract": "noesis-subscription-delivery-worker-v1",
        "worker_id": "maintenance",
        "attempted": 0,
        "delivered": 0,
        "retrying": 0,
        "failed": 0,
        "receipts": [],
    }
    assert conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='knowledge_subscription_outbox'"
    ).fetchone()[0] == 1
    conn.close()
