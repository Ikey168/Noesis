"""Unattended scheduled market-brief generation and webhook delivery.

The worker composes existing pieces instead of adding a second report path:
due schedules run through :meth:`MarketResearchStore.run_due_schedules`, each
brief is exported through :meth:`MarketResearchStore.export_brief` (which
rechecks current provider rights), and the outcome is recorded in the existing
``market_research_deliveries`` state machine. Destinations use the hardened
subscription ``webhook_transport``: an explicitly configured ``NOESIS_*`` URL
environment variable, no redirects, and the delivery key as ``Idempotency-Key``
so a resend after a crash between send and record is deduplicated by the
receiver.

Worker configuration (``market_brief_delivery`` in the maintenance config)::

    {"enabled": true, "principal_id": "svc:market-briefs",
     "scopes": ["market:research:read", "market:research:write",
                "namespace:market:research:write"],
     "namespaces": ["market:research"],
     "destinations": [{"kind": "webhook", "ref": "research-team",
                       "url_env": "NOESIS_MARKET_BRIEF_WEBHOOK_URL"}],
     "output_format": "markdown", "external": false,
     "retry_delay_s": 300, "cooldown_s": 86400, "max_per_tick": 20}
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any

from src.domains.market.research import (
    MarketResearchError,
    MarketResearchStore,
    check_market_brief_accessibility,
)

CONTRACT = "noesis-market-brief-delivery-worker-v1"
PAYLOAD_CONTRACT = "noesis-market-brief-delivery-payload-v1"


class MarketBriefDeliveryWorker:
    def __init__(self, conn: Any, config: Mapping[str, Any], *, transports=None, environ=None, now=None) -> None:
        if not isinstance(config, Mapping) or config.get("enabled") is not True:
            raise MarketResearchError("delivery_disabled", "unattended brief delivery requires explicit opt-in")
        self.principal_id = config.get("principal_id")
        if not isinstance(self.principal_id, str) or not self.principal_id:
            raise MarketResearchError("invalid_worker_auth", "a configured delivery principal is required")
        self.scopes = set(config.get("scopes") or [])
        namespaces = config.get("namespaces")
        if not isinstance(namespaces, list) or not 1 <= len(namespaces) <= 32 or not all(
            isinstance(item, str) and item for item in namespaces
        ):
            raise MarketResearchError("invalid_request", "one to 32 namespaces are required")
        self.namespaces = list(namespaces)
        self.output_format = config.get("output_format", "markdown")
        if self.output_format not in {"json", "markdown", "csv"}:
            raise MarketResearchError("invalid_request", "output_format must be json, markdown or csv")
        self.external = config.get("external", False)
        if type(self.external) is not bool:
            raise MarketResearchError("invalid_request", "external must be boolean")
        self.max_per_tick = config.get("max_per_tick", 20)
        self.retry_delay_ms = int(config.get("retry_delay_s", 300)) * 1000
        self.cooldown_ms = int(config.get("cooldown_s", 86_400)) * 1000
        if type(self.max_per_tick) is not int or not 1 <= self.max_per_tick <= 100:
            raise MarketResearchError("invalid_request", "max_per_tick must be one to 100")
        if not 0 <= self.retry_delay_ms <= 86_400_000 or not 60_000 <= self.cooldown_ms <= 31 * 86_400_000:
            raise MarketResearchError("invalid_request", "retry and cooldown delays are out of bounds")
        destinations = config.get("destinations")
        if not isinstance(destinations, list) or not 1 <= len(destinations) <= 32:
            raise MarketResearchError("invalid_destinations", "one to 32 explicit destinations are required")
        env = environ if environ is not None else os.environ
        self.transports: dict[str, Any] = {}
        for item in destinations:
            if not isinstance(item, Mapping) or item.get("kind") != "webhook":
                raise MarketResearchError("invalid_destinations", "only configured webhook destinations are supported")
            ref, url_env = item.get("ref"), item.get("url_env")
            if (
                not isinstance(ref, str) or not ref
                or not isinstance(url_env, str) or not url_env.startswith("NOESIS_")
                or not url_env.isidentifier()
            ):
                raise MarketResearchError("invalid_destinations", "destination reference and NOESIS_ URL environment name required")
            subscriber = f"webhook:{ref}"
            if subscriber in self.transports:
                raise MarketResearchError("invalid_destinations", "duplicate destination reference")
            if transports is not None:
                if subscriber not in transports:
                    raise MarketResearchError("destination_unavailable", "configured test transport is missing")
                self.transports[subscriber] = transports[subscriber]
            else:
                url = env.get(url_env)
                if not url:
                    raise MarketResearchError("destination_unavailable", "configured webhook URL environment value is unavailable")
                from src.kb.subscription_delivery import webhook_transport

                self.transports[subscriber] = webhook_transport(url, timeout=item.get("timeout_s", 10))
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = MarketResearchStore(conn, now=self.now)
        self.conn = conn

    def readiness(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT,
            "ready": True,
            "principal_id": self.principal_id,
            "namespaces": len(self.namespaces),
            "destinations": sorted(self.transports),
            "external": self.external,
        }

    def _due_retries(self, namespace: str) -> list[tuple[str, int, str]]:
        rows = self.conn.execute(
            "SELECT artifact_id,subscriber_id FROM market_research_deliveries "
            "WHERE namespace=? AND status='retrying' AND next_attempt_ms<=? LIMIT ?",
            [namespace, int(self.now()), self.max_per_tick],
        ).fetchall()
        retries = []
        for report_ref, subscriber in rows:
            report_id, _, version = str(report_ref).rpartition("@")
            if subscriber in self.transports and version.isdigit():
                retries.append((report_id, int(version), subscriber))
        return retries

    def _deliver(self, namespace: str, report_id: str, version: int, subscriber: str) -> dict[str, Any]:
        auth = {"principal_id": self.principal_id, "scopes": self.scopes}
        report = self.store.inspect(namespace, report_id, version, **auth)
        key = self.store.brief_delivery_key(namespace, report, report_id, version, subscriber, self.cooldown_ms)
        prior = self.conn.execute(
            "SELECT status,attempts,next_attempt_ms FROM market_research_deliveries WHERE delivery_key=?",
            [key],
        ).fetchone()
        if prior and not (prior[0] == "retrying" and prior[2] is not None and self.now() >= int(prior[2])):
            # Already delivered/withheld in this cooldown, or a retry not yet due.
            return {
                "report_id": report_id, "version": version, "subscriber_id": subscriber,
                "delivery_key": key, "status": prior[0], "attempts": int(prior[1]),
                "deduplicated": True, "error": None,
            }
        exported = self.store.export_brief(
            namespace, report_id=report_id, version=version, output_format=self.output_format,
            external=self.external, owner=None, **auth,
        )
        if exported["rights"].get("export_withheld"):
            outcome, error = "withheld", "rights_withheld"
        else:
            try:
                self.transports[subscriber](
                    {
                        "contract": PAYLOAD_CONTRACT,
                        "delivery_key": key,
                        "namespace": namespace,
                        "report_id": report_id,
                        "version": version,
                        "export": exported,
                        "accessibility": check_market_brief_accessibility(exported),
                    },
                    idempotency_key=key,
                )
                outcome, error = "delivered", None
            except Exception as exc:  # noqa: BLE001 - transport failure is retried
                outcome, error = "failed", type(exc).__name__
        receipt = self.store.deliver_brief(
            namespace, report_id=report_id, version=version, subscriber_id=subscriber,
            delivery_outcome=outcome, retry_delay_ms=self.retry_delay_ms,
            cooldown_ms=self.cooldown_ms, owner=None, **auth,
        )
        return {
            "report_id": report_id,
            "version": version,
            "subscriber_id": subscriber,
            "delivery_key": receipt["delivery_key"],
            "status": receipt["status"],
            "attempts": receipt["attempts"],
            "deduplicated": receipt["deduplicated"],
            "error": error,
        }

    def tick(self, worker_id: str) -> dict[str, Any]:
        if not isinstance(worker_id, str) or not worker_id:
            raise MarketResearchError("invalid_worker", "worker identity is required")
        generated = []
        deliveries = []
        failures = []
        for namespace in self.namespaces:
            try:
                run = self.store.run_due_schedules(
                    namespace, due_at_ms=int(self.now()), limit=self.max_per_tick, owner=None,
                    principal_id=self.principal_id, scopes=self.scopes,
                )
            except MarketResearchError as exc:
                failures.append({"namespace": namespace, "stage": "schedule", "code": exc.code})
                continue
            targets: list[tuple[str, int, str]] = []
            for item in run["receipts"]:
                generated.append({"namespace": namespace, **item})
                report = self.store.inspect(
                    namespace, item["report_id"], None, principal_id=self.principal_id, scopes=self.scopes
                )
                targets.extend((item["report_id"], int(report["version"]), subscriber) for subscriber in sorted(self.transports))
            targets.extend(self._due_retries(namespace))
            for report_id, version, subscriber in dict.fromkeys(targets):
                try:
                    deliveries.append({"namespace": namespace, **self._deliver(namespace, report_id, version, subscriber)})
                except MarketResearchError as exc:
                    failures.append({"namespace": namespace, "stage": "delivery", "report_id": report_id, "code": exc.code})
        return {
            "contract": CONTRACT,
            "worker_id": worker_id,
            "generated": len(generated),
            "delivered": sum(item["status"] == "delivered" and not item["deduplicated"] for item in deliveries),
            "retrying": sum(item["status"] == "retrying" for item in deliveries),
            "withheld": sum(item["status"] == "withheld" for item in deliveries),
            "failed": len(failures),
            "deliveries": deliveries,
            "failures": failures,
        }


__all__ = ["MarketBriefDeliveryWorker"]
