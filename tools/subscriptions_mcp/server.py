"""Principal-isolated lifecycle and polling for saved knowledge subscriptions."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
mcp = FastMCP("noesis-subscriptions")


def _context() -> tuple[str, set[str]]:
    from src.config.env import resolve_env
    principal=(resolve_env("MCP_PRINCIPAL","local-reader") or "").strip(); raw=resolve_env("MCP_SCOPES","knowledge:subscriptions:read") or ""
    return principal,{item.strip() for item in raw.split(",") if item.strip()}


def _run(operation, *, write: bool = False):
    import duckdb

    from src.config.env import warehouse_path
    from src.kb.subscriptions import SubscriptionError, SubscriptionStore
    conn=None
    try:
        conn=duckdb.connect(warehouse_path() or str(ROOT/"data/neuronews.duckdb"),read_only=not write)
        return operation(SubscriptionStore(conn,initialize=write))
    except SubscriptionError as exc: return {"ok":False,"error":exc.as_dict()}
    except Exception as exc:  # noqa: BLE001 - stable availability envelope
        return {"ok":False,"error":{"code":"subscriptions_unavailable","message":str(exc)[:300]}}
    finally:
        if conn is not None: conn.close()


@mcp.tool()
def subscription_context() -> dict:
    """Return the authenticated owner and effective subscription scopes."""
    principal,scopes=_context(); return {"principal_id":principal,"scopes":sorted(scopes)}


@mcp.tool()
def create_subscription(definition: dict[str, Any], idempotency_key: str) -> dict:
    """Create one deterministic, namespace-scoped saved knowledge query."""
    principal,scopes=_context(); return _run(lambda store:store.create(definition,idempotency_key,principal_id=principal,scopes=scopes),write=True)


@mcp.tool()
def list_subscriptions(namespace: str | None = None) -> dict:
    """List only subscriptions owned by the authenticated principal."""
    principal,scopes=_context(); return _run(lambda store:{"subscriptions":store.list(principal_id=principal,scopes=scopes,namespace=namespace)})


@mcp.tool()
def inspect_subscription(subscription_id: str) -> dict:
    """Inspect an owned subscription and its committed-watermark progress."""
    principal,scopes=_context(); return _run(lambda store:store.inspect(subscription_id,principal_id=principal,scopes=scopes))


@mcp.tool()
def poll_subscription(subscription_id: str, cursor: str = "", limit: int = 100) -> dict:
    """Poll replayable events using an opaque subscription-bound cursor."""
    principal,scopes=_context(); return _run(lambda store:store.poll(subscription_id,principal_id=principal,scopes=scopes,cursor=cursor,limit=limit))


@mcp.tool()
def update_subscription(subscription_id: str, patch: dict[str, Any]) -> dict:
    """Version delivery, cadence, filters, or expiration without replacing identity."""
    principal,scopes=_context(); return _run(lambda store:store.update(subscription_id,patch,principal_id=principal,scopes=scopes),write=True)


@mcp.tool()
def pause_subscription(subscription_id: str) -> dict:
    """Pause evaluation while retaining snapshots and cursor history."""
    principal,scopes=_context(); return _run(lambda store:store.set_status(subscription_id,"paused",principal_id=principal,scopes=scopes),write=True)


@mcp.tool()
def resume_subscription(subscription_id: str) -> dict:
    """Resume evaluation from the last committed watermark."""
    principal,scopes=_context(); return _run(lambda store:store.set_status(subscription_id,"active",principal_id=principal,scopes=scopes),write=True)


@mcp.tool()
def delete_subscription(subscription_id: str) -> dict:
    """Soft-delete a subscription while preserving audit and replay history."""
    principal,scopes=_context(); return _run(lambda store:store.delete(subscription_id,principal_id=principal,scopes=scopes),write=True)


@mcp.tool()
def pending_subscription_deliveries(limit: int = 100) -> dict:
    """Read channel-neutral webhook, email, or queue outbox payloads."""
    principal,scopes=_context(); return _run(lambda store:{"deliveries":store.pending_deliveries(principal_id=principal,scopes=scopes,limit=limit)})


@mcp.tool()
def claim_subscription_deliveries(worker_id: str, limit: int = 100, lease_ms: int = 30000) -> dict:
    """Lease due outbox events to one worker; expired leases are reclaimable."""
    from src.kb.subscription_delivery import SubscriptionDeliveryStore
    principal, scopes = _context()
    return _run(lambda store: {"deliveries": SubscriptionDeliveryStore(store.conn).claim(
        worker_id, principal_id=principal, scopes=scopes, limit=limit, lease_ms=lease_ms)}, write=True)


@mcp.tool()
def acknowledge_subscription_delivery(event_id: str, delivery_kind: str, lease_token: str) -> dict:
    """Acknowledge a current lease after the receiver accepts its stable event ID."""
    from src.kb.subscription_delivery import SubscriptionDeliveryStore
    principal, scopes = _context()
    return _run(lambda store: SubscriptionDeliveryStore(store.conn).finish(
        event_id, delivery_kind, lease_token, principal_id=principal, scopes=scopes), write=True)


@mcp.tool()
def fail_subscription_delivery(event_id: str, delivery_kind: str, lease_token: str,
                                error: str, max_attempts: int = 5, backoff_ms: int = 1000) -> dict:
    """Record a receiver failure and schedule bounded backoff or terminal failure."""
    from src.kb.subscription_delivery import SubscriptionDeliveryStore
    principal, scopes = _context()
    return _run(lambda store: SubscriptionDeliveryStore(store.conn).finish(
        event_id, delivery_kind, lease_token, error=error, max_attempts=max_attempts,
        backoff_ms=backoff_ms, principal_id=principal, scopes=scopes), write=True)


@mcp.tool()
def redrive_subscription_delivery(event_id: str, delivery_kind: str, request_key: str) -> dict:
    """Idempotently return a terminal failure to the delivery queue."""
    from src.kb.subscription_delivery import SubscriptionDeliveryStore
    principal, scopes = _context()
    return _run(lambda store: SubscriptionDeliveryStore(store.conn).redrive(
        event_id, delivery_kind, request_key, principal_id=principal, scopes=scopes), write=True)


if __name__ == "__main__":
    from src.mcp_host.transport import run_server
    run_server(mcp)
