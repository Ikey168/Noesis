from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import duckdb
import pytest

from src.kb.subscriptions import SubscriptionStore, SubscriptionError
from src.kb.subscription_delivery import SubscriptionDeliveryStore, deliver_once

SCOPES = {"knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:subscriptions:deliver", "namespace:research:read"}
AUTH = {"principal_id": "alice", "scopes": SCOPES}


def setup(conn, *, coverage=None):
    subscriptions = SubscriptionStore(conn)
    sid = subscriptions.create({"namespace": "research", "query": {"operation": "search"},
                                "delivery": {"kind": "webhook", "destination_ref": "receiver"}}, "one", **AUTH)["subscription_id"]
    subscriptions.commit_watermark("research", 1)
    subscriptions.evaluate(sid, 1, {"items": [{"id": "a"}], "coverage": {"complete": True}}, observed_at_ms=10, **AUTH)
    return subscriptions, sid


@pytest.mark.parametrize("coverage,filters,reason,confirmed", [
    ({"complete": False}, None, "incomplete-coverage", False),
    ({"complete": True}, {"language": "de"}, "filter-changed", False),
    ({"complete": True, "top_k": 10}, None, "possible-top-k-displacement", False),
    ({"complete": True}, None, "result-set-absence", False),
    ({"complete": True, "removals": {"a": {"reason": "retracted", "revision_id": "r2"}}}, None, "confirmed-retracted", True),
])
def test_removal_reasons_are_visible_to_poll_and_push(coverage, filters, reason, confirmed):
    conn = duckdb.connect()
    store, sid = setup(conn)
    if filters:
        store.update(sid, {"filters": filters}, **AUTH)
    store.commit_watermark("research", 2)
    store.evaluate(sid, 2, {"items": [], "coverage": coverage}, **AUTH)
    removed = next(e for e in store.poll(sid, **AUTH)["events"] if e["event_type"] == "removed")
    assert removed["evidence"]["removal"]["reason"] == reason
    assert removed["evidence"]["removal"]["withdrawal_confirmed"] is confirmed
    pushed = next(e["payload"] for e in store.pending_deliveries(**AUTH) if e["payload"]["event_type"] == "removed")
    assert pushed["evidence"] == removed["evidence"]


def test_lease_expiry_crash_after_send_and_receiver_deduplication(tmp_path):
    path = str(tmp_path / "delivery.duckdb")
    conn = duckdb.connect(path)
    setup(conn)
    clock = [100]
    store = SubscriptionDeliveryStore(conn, now=lambda: clock[0])
    first = store.claim("worker-1", lease_ms=1000, **AUTH)[0]
    assert store.claim("worker-2", **AUTH) == []
    received = {first["event_id"]}  # receiver accepted, sender crashes before ack
    conn.close()
    conn = duckdb.connect(path)
    clock[0] = 1101
    store = SubscriptionDeliveryStore(conn, now=lambda: clock[0])
    second = store.claim("worker-2", **AUTH)[0]
    assert second["event_id"] in received and second["attempts"] == 2
    with pytest.raises(SubscriptionError, match="replaced"):
        store.finish(first["event_id"], "webhook", first["lease_token"], **AUTH)
    assert store.finish(second["event_id"], "webhook", second["lease_token"], **AUTH)["status"] == "delivered"
    assert store.finish(second["event_id"], "webhook", second["lease_token"], **AUTH)["idempotent"]
    assert store.pending(**AUTH) == []
    conn.close()


def test_backoff_terminal_failure_redrive_and_owner_access():
    conn = duckdb.connect()
    subscriptions, sid = setup(conn)
    clock = [100]
    store = SubscriptionDeliveryStore(conn, now=lambda: clock[0])
    for attempt in range(2):
        item = store.claim("worker", **AUTH)[0]
        result = store.finish(item["event_id"], "webhook", item["lease_token"], error="receiver unavailable", max_attempts=2, **AUTH)
        assert store.finish(item["event_id"], "webhook", item["lease_token"], error="receiver unavailable", **AUTH)["idempotent"]
        assert store.claim("worker", **AUTH) == []
        clock[0] = result["available_at_ms"]
    assert result["status"] == "failed"
    for auth in [{**AUTH, "principal_id": "bob"}, {**AUTH, "scopes": SCOPES - {"namespace:research:read"}}]:
        with pytest.raises(SubscriptionError):
            store.redrive(item["event_id"], "webhook", "retry", **auth)
    assert not store.redrive(item["event_id"], "webhook", "retry", **AUTH)["idempotent"]
    assert store.redrive(item["event_id"], "webhook", "retry", **AUTH)["idempotent"]
    received = []
    transport = lambda payload, **kw: received.append((payload, kw))
    assert deliver_once(store, "worker", {("webhook", "receiver"): transport}, **AUTH)[0]["status"] == "delivered"
    assert received[0][1]["idempotency_key"] == item["event_id"]


def test_competing_workers_claim_each_event_once(tmp_path):
    path = str(tmp_path / "competing.duckdb")
    conn = duckdb.connect(path)
    setup(conn)
    barrier = Barrier(2)
    def claim(worker):
        connection = duckdb.connect(path)
        store = SubscriptionDeliveryStore(connection, now=lambda: 100, initialize=False)
        barrier.wait(timeout=5)
        try:
            return store.claim(worker, **AUTH)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["one", "two"]))
    assert sum(map(len, results)) == 1
    conn.close()
