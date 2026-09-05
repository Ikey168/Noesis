"""Leased at-least-once delivery for the existing subscription outbox."""

import json
import secrets
import time

from src.kb.subscriptions import DELIVER_SCOPE, SubscriptionError, _scope


def ensure_schema(conn):
    for name, kind in (("lease_token", "TEXT"), ("lease_owner", "TEXT"),
                       ("lease_until_ms", "BIGINT"), ("last_error", "TEXT"), ("last_outcome", "TEXT")):
        conn.execute(f"ALTER TABLE knowledge_subscription_outbox ADD COLUMN IF NOT EXISTS {name} {kind}")
    conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_subscription_redrives(
        event_id TEXT NOT NULL,delivery_kind TEXT NOT NULL,request_key TEXT NOT NULL,
        PRIMARY KEY(event_id,delivery_kind,request_key))""")


class SubscriptionDeliveryStore:
    def __init__(self, conn, *, now=None, initialize=True):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_schema(conn)

    def _owned(self, event_id, delivery_kind, principal_id, scopes):
        _scope(scopes, DELIVER_SCOPE)
        row = self.conn.execute("""SELECT o.status,o.attempts,o.lease_token,o.lease_until_ms,
            o.last_outcome,s.namespace,s.owner_principal FROM knowledge_subscription_outbox o
            JOIN knowledge_subscription_events e ON e.event_id=o.event_id
            JOIN knowledge_subscriptions s ON s.subscription_id=e.subscription_id
            WHERE o.event_id=? AND o.delivery_kind=?""", [event_id, delivery_kind]).fetchone()
        if not row or row[6] != principal_id:
            raise SubscriptionError("not_found", "owned delivery does not exist")
        _scope(scopes, DELIVER_SCOPE, row[5])
        return row

    def pending(self, *, principal_id, scopes, limit=100):
        _scope(scopes, DELIVER_SCOPE)
        rows = self.conn.execute("""SELECT o.event_id,o.delivery_kind,o.destination_ref,o.payload_json,
            o.attempts,s.namespace FROM knowledge_subscription_outbox o
            JOIN knowledge_subscription_events e ON e.event_id=o.event_id
            JOIN knowledge_subscriptions s ON s.subscription_id=e.subscription_id
            WHERE s.owner_principal=? AND s.status <> 'deleted' AND
            ((o.status='pending' AND o.available_at_ms<=?) OR (o.status='leased' AND o.lease_until_ms<=?))
            ORDER BY e.sequence LIMIT ?""", [principal_id, self.now(), self.now(), min(max(int(limit), 1), 500)]).fetchall()
        result = []
        for event_id, kind, destination, payload, attempts, namespace in rows:
            try:
                _scope(scopes, DELIVER_SCOPE, namespace)
            except SubscriptionError:
                continue
            result.append({"event_id": event_id, "delivery_kind": kind, "destination_ref": destination,
                           "payload": json.loads(payload), "attempts": attempts})
        return result

    def claim(self, worker_id, *, principal_id, scopes, limit=100, lease_ms=30000):
        if not isinstance(worker_id, str) or not worker_id:
            raise SubscriptionError("invalid_worker", "worker identity is required")
        lease_ms = min(max(int(lease_ms), 1000), 300000)
        claimed = []
        for item in self.pending(principal_id=principal_id, scopes=scopes, limit=limit):
            token, now = secrets.token_hex(24), self.now()
            try:
                row = self.conn.execute("""UPDATE knowledge_subscription_outbox SET
                    status='leased',lease_token=?,lease_owner=?,lease_until_ms=?,attempts=attempts+1,last_outcome=NULL
                    WHERE event_id=? AND delivery_kind=? AND
                    ((status='pending' AND available_at_ms<=?) OR (status='leased' AND lease_until_ms<=?))
                    RETURNING attempts""", [token, worker_id, now + lease_ms, item["event_id"], item["delivery_kind"], now, now]).fetchone()
            except Exception as exc:
                import duckdb
                if isinstance(exc, duckdb.TransactionException):
                    continue  # another worker won this lease
                raise
            if row:
                claimed.append({**item, "attempts": int(row[0]), "lease_token": token, "lease_until_ms": now + lease_ms})
        return claimed

    def finish(self, event_id, delivery_kind, lease_token, *, principal_id, scopes,
               error=None, max_attempts=5, backoff_ms=1000):
        row = self._owned(event_id, delivery_kind, principal_id, scopes)
        outcome = "ack" if error is None else "failure"
        if not lease_token or row[2] != lease_token:
            raise SubscriptionError("lease_conflict", "delivery lease was replaced")
        if row[4] == outcome:
            return {"event_id": event_id, "status": row[0], "idempotent": True}
        now = self.now()
        if row[0] != "leased" or row[3] <= now:
            raise SubscriptionError("lease_expired", "a current delivery lease is required")
        if error is None:
            status, available, delivered = "delivered", now, now
        else:
            status = "failed" if row[1] >= min(max(int(max_attempts), 1), 100) else "pending"
            available = now + min(3600000, max(1, int(backoff_ms)) * 2 ** min(row[1] - 1, 20))
            delivered = None
        changed = self.conn.execute("""UPDATE knowledge_subscription_outbox SET status=?,available_at_ms=?,
            delivered_at_ms=?,last_error=?,last_outcome=? WHERE event_id=? AND delivery_kind=?
            AND status='leased' AND lease_token=? AND lease_until_ms>? RETURNING event_id""",
            [status, available, delivered, None if error is None else str(error)[:500], outcome,
             event_id, delivery_kind, lease_token, now]).fetchone()
        if not changed:
            raise SubscriptionError("lease_conflict", "delivery lease changed")
        return {"event_id": event_id, "status": status, "available_at_ms": available, "idempotent": False}

    def redrive(self, event_id, delivery_kind, request_key, *, principal_id, scopes):
        if not isinstance(request_key, str) or not request_key:
            raise SubscriptionError("invalid_request_key", "redrive requires an idempotency key")
        self.conn.execute("BEGIN TRANSACTION")
        try:
            row = self._owned(event_id, delivery_kind, principal_id, scopes)
            prior = self.conn.execute("SELECT 1 FROM knowledge_subscription_redrives WHERE event_id=? AND delivery_kind=? AND request_key=?", [event_id, delivery_kind, request_key]).fetchone()
            if not prior:
                if row[0] != "failed":
                    raise SubscriptionError("invalid_status", "only terminal failures can be redriven")
                self.conn.execute("""UPDATE knowledge_subscription_outbox SET status='pending',attempts=0,
                    available_at_ms=?,lease_token=NULL,lease_owner=NULL,lease_until_ms=NULL,last_outcome=NULL
                    WHERE event_id=? AND delivery_kind=?""", [self.now(), event_id, delivery_kind])
                self.conn.execute("INSERT INTO knowledge_subscription_redrives VALUES (?,?,?)", [event_id, delivery_kind, request_key])
            self.conn.execute("COMMIT")
            return {"event_id": event_id, "status": "redriven", "idempotent": bool(prior)}
        except Exception:
            self.conn.execute("ROLLBACK")
            raise


def deliver_once(store, worker_id, transports, *, principal_id, scopes, limit=100):
    """Destinations must be explicitly configured by reference, not supplied by payload URLs."""
    results = []
    # Acquire immediately before sending, so queued batch members do not lose
    # their leases while the worker is waiting on earlier receivers.
    for _ in range(min(max(int(limit), 0), 500)):
        claimed = store.claim(worker_id, principal_id=principal_id, scopes=scopes, limit=1)
        if not claimed:
            break
        item = claimed[0]
        error = None
        try:
            transport = transports[(item["delivery_kind"], item["destination_ref"])]
            transport(item["payload"], idempotency_key=item["event_id"])
        except Exception as exc:
            error = str(exc)
        results.append(store.finish(item["event_id"], item["delivery_kind"], item["lease_token"],
                                    principal_id=principal_id, scopes=scopes, error=error))
    return results


def webhook_transport(url, *, timeout=10):
    """Build an explicitly configured HTTP destination; receivers deduplicate event IDs."""
    from urllib.parse import urlsplit
    from urllib.request import Request, build_opener, HTTPRedirectHandler
    if urlsplit(url).scheme not in {"http", "https"} or not 0 < timeout <= 20:
        raise ValueError("HTTP(S) destination and timeout in (0,20] seconds are required")

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = build_opener(NoRedirect)

    def send(payload, *, idempotency_key):
        request = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Idempotency-Key": idempotency_key}, method="POST")
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"receiver returned HTTP {response.status}")
    return send
