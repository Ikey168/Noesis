"""Explicitly configured unattended delivery of committed subscription events."""

from __future__ import annotations

import os
from collections.abc import Mapping

from src.kb.subscription_delivery import SubscriptionDeliveryStore, deliver_once, webhook_transport
from src.kb.subscriptions import DELIVER_SCOPE, SubscriptionError, SubscriptionStore

CONTRACT = "noesis-subscription-delivery-worker-v1"


class SubscriptionDeliveryWorker:
    def __init__(self, conn, config: Mapping, *, transports=None, environ=None, now=None):
        if not isinstance(config, Mapping) or config.get("enabled") is not True:
            raise SubscriptionError("delivery_disabled", "unattended delivery requires explicit opt-in")
        self.principal_id = config.get("principal_id")
        self.scopes = set(config.get("scopes") or [])
        if (not isinstance(self.principal_id, str) or not self.principal_id or
            DELIVER_SCOPE not in self.scopes and "operator" not in self.scopes):
            raise SubscriptionError("invalid_worker_auth", "configured delivery principal and scope required")
        self.max_per_tick = config.get("max_per_tick", 20)
        if type(self.max_per_tick) is not int or not 1 <= self.max_per_tick <= 100:
            raise SubscriptionError("invalid_worker_limit", "max_per_tick must be one to 100")
        destinations = config.get("destinations")
        if not isinstance(destinations, list) or not 1 <= len(destinations) <= 32:
            raise SubscriptionError("invalid_destinations", "one to 32 explicit destinations required")
        env = environ if environ is not None else os.environ
        configured = {}
        for item in destinations:
            if not isinstance(item, Mapping) or item.get("kind") != "webhook":
                raise SubscriptionError("invalid_destinations", "only configured webhook destinations are supported")
            ref, url_env = item.get("ref"), item.get("url_env")
            if (not isinstance(ref, str) or not ref or
                not isinstance(url_env, str) or not url_env.startswith("NOESIS_") or
                not url_env.isidentifier()):
                raise SubscriptionError("invalid_destinations", "destination reference and NOESIS_ URL environment name required")
            key = ("webhook", ref)
            if key in configured:
                raise SubscriptionError("invalid_destinations", "duplicate destination reference")
            if transports is not None:
                if key not in transports:
                    raise SubscriptionError("destination_unavailable", "configured test transport is missing")
                configured[key] = transports[key]
            else:
                url = env.get(url_env)
                if not url:
                    raise SubscriptionError("destination_unavailable", "configured webhook URL environment value is unavailable")
                configured[key] = webhook_transport(url, timeout=item.get("timeout_s", 10))
        self.transports = configured
        # The delivery store extends the outbox with lease columns, so it
        # requires the base subscription schema to exist first. An enabled
        # worker is also the migration boundary for an otherwise empty
        # warehouse; the default disabled path still performs no setup.
        SubscriptionStore(conn)
        self.store = SubscriptionDeliveryStore(conn, now=now)

    def readiness(self):
        return {"contract": CONTRACT, "ready": True,
                "principal_id": self.principal_id,
                "destinations": [{"kind": kind, "ref": ref} for kind, ref in sorted(self.transports)],
                "max_per_tick": self.max_per_tick}

    def tick(self, worker_id):
        if not isinstance(worker_id, str) or not worker_id:
            raise SubscriptionError("invalid_worker", "worker identity is required")
        results = deliver_once(
            self.store, worker_id, self.transports,
            principal_id=self.principal_id, scopes=self.scopes,
            limit=self.max_per_tick)
        return {"contract": CONTRACT, "worker_id": worker_id,
                "attempted": len(results), "delivered": sum(item["status"] == "delivered" for item in results),
                "retrying": sum(item["status"] == "pending" for item in results),
                "failed": sum(item["status"] == "failed" for item in results),
                "receipts": results}
