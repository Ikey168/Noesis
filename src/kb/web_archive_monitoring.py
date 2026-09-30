"""Monitor cited URLs for new captures and link rot through subscriptions (#2226, WA12).

A web-archive monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`). Its query names a cited URL
and, optionally, the citation whose pin it guards. There is no monitor table and
no new scheduler. :meth:`WebArchiveMonitor.check` makes one bounded round within
the WA01 budgets: one Memento resolution, one request to the live URL and one
request per pinned capture. Observations go to the existing capture, TimeMap
and citation-health records. :meth:`WebArchiveMonitor.run` evaluates a
committed watermark and turns differences into subscription events delivered
through the existing poll and outbox paths.

Notifications (``noesis-web-archive-notification-v1``):

- ``new_capture``: the URL gained a capture; carries the capture record id.
- ``live_url_failure``: the live URL stopped resolving while a pinned capture
  exists; carries the citation, pin and health record ids.
- ``pinned_capture_unavailable``: the pinned capture stopped resolving in its
  archive; carries the pin, capture and health record ids.

A link-rot notification reports the HTTP outcome as observed (for example
"HTTP 404 observed") and never draws a conclusion about the publisher. An
unchanged TimeMap emits nothing. The first evaluation is a baseline and emits
no notifications.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

CONTRACT = "noesis-web-archive-notification-v1"
EVENT_KINDS = ("new_capture", "live_url_failure", "pinned_capture_unavailable")
_UNAVAILABLE = {"unavailable", "soft-404", "takedown"}


class WebArchiveMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        from src.kb.citation_preservation import CitationPreservationStore
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = CitationPreservationStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, url: str, *, principal_id: str, scopes: Iterable[str],
               citation_id: str | None = None, delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        from src.ingestion.memento import validate_url
        from src.kb.citation_preservation import READ_SCOPE, _require

        scopes = set(scopes)
        _require(scopes, READ_SCOPE)
        query = {"operation": "search", "kind": "web-archive-monitor", "url": validate_url(url),
                 **({"citation_id": citation_id} if citation_id else {})}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "web-archives", "query": query, "filters": {},
             "cadence": {"trigger": "watermark"}, "delivery": delivery or {"kind": "poll"}},
            "web-archive-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "check() records bounded observations; the maintenance orchestrator commits "
                                      "the watermarks run() evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        from src.kb.citation_preservation import CitationPreservationError

        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "web-archive-monitor":
            raise CitationPreservationError("monitor_not_found", "subscription is not a web-archive monitor")
        return subscription

    @staticmethod
    def _health_key(query: Mapping[str, Any]) -> str:
        return query.get("citation_id") or "url:" + query["url"]

    # ------------------------------------------------------------------ bounded checks

    def check(self, subscription_id: str, *, request_id: str, principal_id: str, scopes: Iterable[str],
              transport: Callable[..., Mapping[str, Any]] | None = None, via_aggregator: bool = True,
              archives: Iterable[str] | None = None, live_check: bool = True,
              evidence_origin: str | None = None) -> dict[str, Any]:
        """One bounded round: a resolution, the live URL and each pinned capture; observations are recorded."""
        from src.ingestion.memento import Budget, MementoClient

        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace, query = subscription["namespace"], subscription["query"]
        client = MementoClient(self.conn, namespace, principal_id=principal_id, scopes=scopes, transport=transport,
                               evidence_origin=evidence_origin, now=self.now)
        resolution = client.resolve(query["url"], request_id=request_id, via_aggregator=via_aggregator,
                                    archives=list(archives) if archives is not None else None)
        observed: dict[str, Any] = {"resolution": resolution["request_id"], "live": None, "pinned": []}

        def probe(target: str, label: str) -> tuple[int, str]:
            budget = Budget(label, client.transport, max_requests=1, max_bytes=65_536)
            try:
                status, _, _ = budget.get(target)
            except Exception as exc:  # noqa: BLE001 - an oversize or failed probe is an observation
                return 0, type(exc).__name__
            failure = budget.requests[-1].get("failure_type")
            return status, failure or ""

        if live_check:
            status, failure = probe(query["url"], "live-url")
            health = self.store.record_health(namespace, self._health_key(query), query["url"], status,
                                              principal_id=principal_id, scopes=scopes)
            observed["live"] = {"health_id": health["health_id"], "http_status": status or None,
                                "transport_failure": failure or None}
        if query.get("citation_id"):
            pins = self.store.pins(namespace, query["citation_id"], scopes=scopes)
            if pins and pins[-1]["capture"]["uri_m"].startswith(("http://", "https://")):
                uri_m = pins[-1]["capture"]["uri_m"]
                status, failure = probe(uri_m, "pinned-capture")
                health = self.store.record_health(namespace, query["citation_id"], uri_m, status,
                                                  principal_id=principal_id, scopes=scopes)
                observed["pinned"].append({"pin_id": pins[-1]["pin_id"], "health_id": health["health_id"],
                                           "http_status": status or None, "transport_failure": failure or None})
        return {"subscription_id": subscription_id, "observed": observed,
                "archives": {k: v["outcome"] for k, v in resolution["archives"].items()}}

    # ------------------------------------------------------------------ snapshots

    def _health(self, namespace: str, citation_key: str, url: str) -> dict[str, Any] | None:
        """Latest health observation for a URL, cited by the first check of its current streak."""
        rows = self.conn.execute(
            "SELECT payload_json FROM citation_health_checks WHERE namespace=? AND citation_id=? "
            "ORDER BY checked_at_ms DESC, health_id DESC LIMIT 500", [namespace, citation_key]).fetchall()
        streak = []
        for row in rows:
            value = json.loads(row[0])
            if value["url"] != url:
                continue
            if streak and value["status"] != streak[0]["status"]:
                break
            streak.append(value)
        if not streak:
            return None
        return {"status": streak[0]["status"], "http_status": streak[0]["http_status"],
                "health_id": streak[-1]["health_id"], "latest_health_id": streak[0]["health_id"],
                "first_observed_at_ms": streak[-1]["checked_at_ms"]}

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> dict[str, Any]:
        namespace, query = subscription["namespace"], subscription["query"]
        items: list[dict[str, Any]] = []
        for capture in self.store.captures_for_url(namespace, query["url"], scopes=scopes):
            items.append({"id": "capture:" + capture["capture_id"], "kind": "capture",
                          "cite": {"capture_id": capture["capture_id"], "archive_id": capture["archive_id"],
                                   "uri_m": capture["uri_m"]},
                          "summary": {"memento_datetime": capture["memento_datetime"],
                                      "archive_id": capture["archive_id"]}})
        live = self._health(namespace, self._health_key(query), query["url"])
        if live:
            items.append({"id": "live:" + query["url"], "kind": "live_url",
                          "cite": {"health_id": live["health_id"], "citation_id": query.get("citation_id"),
                                   "url": query["url"]},
                          "summary": {"status": live["status"], "http_status": live["http_status"]}})
        if query.get("citation_id"):
            pins = self.store.pins(namespace, query["citation_id"], scopes=scopes)
            if pins:
                pin = pins[-1]
                health = self._health(namespace, query["citation_id"], pin["capture"]["uri_m"])
                items.append({"id": "pinned:" + pin["pin_id"], "kind": "pinned_capture",
                              "cite": {"pin_id": pin["pin_id"], "capture_id": pin["capture_id"],
                                       "citation_id": pin["citation_id"], "uri_m": pin["capture"]["uri_m"],
                                       "archive_id": pin["capture"]["archive_id"],
                                       "health_id": health["health_id"] if health else None},
                              "summary": {"status": health["status"] if health else "unchecked",
                                          "http_status": health["http_status"] if health else None}})
        return {"items": items, "coverage": {"complete": True}}

    # ------------------------------------------------------------------ evaluation

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.citation_preservation import CitationPreservationError

        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        if watermark is None:
            row = self.conn.execute("SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                                    [namespace]).fetchone()
            if row is None or row[0] is None:
                raise CitationPreservationError("watermark_uncommitted", "no committed watermark yet")
            watermark = int(row[0])
        baseline = subscription["last_watermark"] is None
        result = self.snapshot(subscription, scopes)
        pinned = next((i for i in result["items"] if i["kind"] == "pinned_capture"), None)
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        if not baseline:
            for event_id in evaluated.get("event_ids", []):
                row = self.conn.execute("SELECT object_key, before_json, after_json FROM "
                                        "knowledge_subscription_events WHERE event_id=?", [event_id]).fetchone()
                notifications.extend(self._classify(event_id, row[0], row[1], row[2], pinned))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark,
                "baseline": baseline, "events": evaluated.get("events", 0), "notifications": notifications,
                "delivery": subscription["delivery"]}

    @staticmethod
    def _observed(summary: Mapping[str, Any]) -> str:
        code = summary.get("http_status")
        return f"HTTP {code} observed" if code else "no HTTP response observed"

    def _classify(self, event_id: str, key: str, before: str | None, after: str | None,
                  pinned: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        old = json.loads(before) if before else None
        new = json.loads(after) if after else None
        if new is None:
            return []

        def note(kind: str, message: str, cites: Mapping[str, Any]) -> dict[str, Any]:
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": kind, "object": key, "message": message, "summary": new["summary"],
                    "previous_summary": (old or {}).get("summary"), "cites": dict(cites),
                    "interpretation": "outcome as observed; no conclusion about the publisher"}

        if new["kind"] == "capture" and old is None:
            return [note("new_capture", f"new capture in {new['summary']['archive_id']} at "
                                        f"{new['summary']['memento_datetime']}", new["cite"])]
        was_ok = old is None or old["summary"]["status"] not in _UNAVAILABLE
        now_failing = new["summary"]["status"] in _UNAVAILABLE
        if new["kind"] == "live_url" and pinned and was_ok and now_failing:
            return [note("live_url_failure", f"{self._observed(new['summary'])} at the live URL; the pinned "
                                             f"capture {pinned['cite']['uri_m']} stays cited",
                         {**new["cite"], "pin_id": pinned["cite"]["pin_id"],
                          "capture_id": pinned["cite"]["capture_id"]})]
        if new["kind"] == "pinned_capture" and was_ok and now_failing:
            return [note("pinned_capture_unavailable", f"{self._observed(new['summary'])} for the pinned capture "
                                                       f"in {new['cite']['archive_id']}", new["cite"])]
        return []

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = ""
             ) -> dict[str, Any]:
        scopes = set(scopes)
        self._subscription(subscription_id, principal_id, scopes)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)
