import asyncio

import duckdb

from src.kb.subscriptions import SubscriptionStore
from src.mcp_host.catalog import _mutability, _required_scopes
from tools.subscriptions_mcp import server


def test_subscription_delivery_tools_auth_and_lifecycle(tmp_path, monkeypatch):
    path = tmp_path / "subscriptions.duckdb"
    scopes = {"knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:subscriptions:deliver", "namespace:research:read"}
    monkeypatch.setenv("NOESIS_WAREHOUSE_PATH", str(path))
    monkeypatch.setattr("src.config.env.warehouse_path", lambda: str(path))
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    conn = duckdb.connect(str(path))
    store = SubscriptionStore(conn)
    sid = store.create({"namespace": "research", "query": {"operation": "objects"}, "delivery": {"kind": "queue", "destination_ref": "fake"}}, "one", principal_id="alice", scopes=scopes)["subscription_id"]
    store.commit_watermark("research", 1)
    store.evaluate(sid, 1, {"items": [{"id": "a"}]}, principal_id="alice", scopes=scopes)
    conn.close()
    tools = asyncio.run(server.mcp.get_tools())
    item = tools["claim_subscription_deliveries"].fn(worker_id="worker")["deliveries"][0]
    identity = {key: item[key] for key in ("event_id", "delivery_kind", "lease_token")}
    assert tools["fail_subscription_delivery"].fn(**identity, error="unavailable", max_attempts=1)["status"] == "failed"
    assert tools["redrive_subscription_delivery"].fn(event_id=item["event_id"], delivery_kind="queue", request_key="retry")["status"] == "redriven"
    item = tools["claim_subscription_deliveries"].fn(worker_id="worker")["deliveries"][0]
    identity["lease_token"] = item["lease_token"]
    assert tools["acknowledge_subscription_delivery"].fn(**identity)["status"] == "delivered"
    scopes.remove("namespace:research:read")
    assert tools["acknowledge_subscription_delivery"].fn(**identity)["error"]["code"] == "unauthorized"
    for name in ("claim_subscription_deliveries", "acknowledge_subscription_delivery", "fail_subscription_delivery", "redrive_subscription_delivery"):
        assert _mutability(name) == "write"
        assert _required_scopes("subscriptions_mcp", "write", name) == ["knowledge:subscriptions:deliver"]
