"""Monitor advertisers, elections and platforms through subscriptions (#2580, SP11).

A platform-transparency monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), following
:mod:`src.kb.campaign_finance_monitoring`: its query names an advertiser (a
Meta page id, a Google advertiser id, a subject key, a declared name, or a
record another pack owns reached through accepted identity decisions), an
election (SP08 links) or a platform (its DSA dump releases). There is no
monitor table and no scheduler: the ``bounded-public-osint`` source-pack
schedule (left disabled; runs are explicit) acquires, the maintenance
orchestrator commits the watermarks, and each evaluation turns differences into
subscription events delivered through the existing poll and outbox paths.

Notices are record changes, not assessments. Each cites the new (and the
previous) record revision and states what changed: ``ad_published``,
``ad_ranges_revised`` (spend and impression ranges as published, before and
after), ``ad_revised``, ``ad_not_returned`` (the removal revision and its
listing), ``ad_relisted``, ``dump_released`` and ``dump_republished`` (the
published SHA-1 before and after). A notice never carries a point estimate,
a user identifier or a coordination reading.

A revision acquired *live* from a provider whose access is still
``unverified-live`` is withheld until a dated live run verifies it; fixture
replays are notified and marked as fixture evidence.
:meth:`PlatformTransparencyMonitor.refresh` re-reads one declared source
selection through the real adapter within its page budget; re-reading unchanged
responses adds nothing and every unit leaves a receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.platform_transparency_sources import LIVE_VERIFICATION, slug
from src.kb.platform_transparency_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    PlatformTransparencyError,
    PlatformTransparencyStore,
    authorize,
)

CONTRACT = "noesis-platform-transparency-notification-v1"
WATCH_KINDS = ("advertiser", "election", "platform")


def notifiable(row: Mapping[str, Any]) -> bool:
    provider = row.get("provider") or row["record"].get("provider")
    return (row["evidence_origin"] == "fixture"
            or LIVE_VERIFICATION.get(provider, {}).get("status") == "verified-live")


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = row["record"]["fields"]
    if row["record_kind"] == "dump-release":
        return {"platform": fields.get("platform"), "date": fields.get("date"), "version": fields.get("version"),
                "sha1_as_published": fields.get("sha1_as_published"), "statements": fields.get("statements")}
    return {"ad_id": fields.get("ad_id"), "advertiser_as_declared": fields.get("advertiser_as_declared"),
            "funding_entity_as_declared": fields.get("funding_entity_as_declared"),
            "delivery_start": fields.get("ad_delivery_start_time") or fields.get("date_range_start"),
            "delivery_stop": fields.get("ad_delivery_stop_time") or fields.get("date_range_end"),
            "spend_range_as_published": fields.get("spend"),
            "impressions_range_as_published": fields.get("impressions"),
            "listing_state": fields.get("listing_state"),
            "not_returned_basis": fields.get("not_returned_basis")}


class PlatformTransparencyMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = PlatformTransparencyStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = str(key or "").strip()
        if watch not in WATCH_KINDS or not key:
            raise PlatformTransparencyError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "platform":
            key = slug(key)
        query = {"operation": "search", "kind": "platform-transparency-monitor", "watch": watch, "key": key}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "osint", "query": query, "filters": {"watch": watch},
             "cadence": {"trigger": "watermark"}, "delivery": delivery or {"kind": "poll"}},
            "platform-transparency-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "explicit bounded-public-osint source-pack runs and the maintenance "
                                      "orchestrator commit the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "platform-transparency-monitor":
            raise PlatformTransparencyError("monitor_not_found", "subscription is not a platform-transparency monitor")
        return subscription

    def _rows(self, subscription: Mapping[str, Any], scopes: set[str]) -> list[dict[str, Any]]:
        namespace, query = subscription["namespace"], subscription["query"]
        if query["watch"] == "platform":
            return self.store.records(namespace, scopes=scopes, kinds=["dump-release"], platform=query["key"])
        if query["watch"] == "election":
            from src.kb.platform_transparency_links import PlatformTransparencyLinks

            links = PlatformTransparencyLinks(self.conn, initialize=False).links(namespace, scopes=scopes,
                                                                                 kind="election",
                                                                                 target_key=query["key"])
            keys = sorted({link["record_key"] for link in links})
            return self.store.records(namespace, scopes=scopes, record_keys=keys) if keys else []
        from src.kb.platform_transparency_links import ad_keys_of
        from src.kb.platform_transparency_queries import PlatformTransparencyQueries

        subjects, _ = PlatformTransparencyQueries(self.conn).subjects_for(namespace, query["key"], scopes)
        keys = sorted({k for s in subjects for k in ad_keys_of(self.store, namespace, s, scopes)})
        return self.store.records(namespace, scopes=scopes, kinds=["ad"], record_keys=keys) if keys else []

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        items, withheld = [], 0
        for row in self._rows(subscription, scopes):
            if not notifiable(row):
                withheld += 1
                continue
            items.append({
                "id": f"{row['source_id']}:{row['record_key']}", "kind": row["record_kind"],
                "record_key": row["record_key"], "source_id": row["source_id"], "provider": row["provider"],
                "platform": row["platform"], "revision_id": row["revision_id"], "revision_no": row["revision_no"],
                "change": row["change"], "source_as_of": row["source_as_of"],
                "evidence_origin": row["evidence_origin"], "summary": _summary(row),
            })
        return {"items": items, "coverage": {"complete": True}}, withheld

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            row = self.conn.execute("SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                                    [namespace]).fetchone()
            if row is None or row[0] is None:
                raise PlatformTransparencyError("watermark_uncommitted", "no committed watermark yet; source-pack "
                                                                         "runs and the maintenance orchestrator "
                                                                         "commit them")
            watermark = int(row[0])
        result, withheld = self.snapshot(subscription, scopes)
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute("SELECT event_type, object_key, before_json, after_json FROM "
                                    "knowledge_subscription_events WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark,
                "notifications": notifications, "withheld_unverified_live_revisions": withheld,
                "delivery": subscription["delivery"]}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, before: str | None, after: str | None
                  ) -> list[dict[str, Any]]:
        old = json.loads(before) if before else None
        new = json.loads(after) if after else None
        if event_type == "removed" or new is None or (old and old["revision_id"] == new["revision_id"]):
            return []

        def note(kind: str, message: str, **detail: Any) -> dict[str, Any]:
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": kind, "object": key, "record_key": new["record_key"], "message": message,
                    "cites": {"record_key": new["record_key"], "revision_id": new["revision_id"],
                              "previous_revision_id": old["revision_id"] if old else None,
                              "source_id": new["source_id"], "provider": new["provider"],
                              "source_as_of": new["source_as_of"]},
                    "evidence_origin": new["evidence_origin"], **detail}

        summary, prior = new["summary"], (old or {}).get("summary") or {}
        label = new["record_key"]
        if new["kind"] == "dump-release":
            if old is None:
                return [note("dump_released", f"{label}: dump {summary.get('date')} ({summary.get('version')}) "
                                              f"published with SHA-1 {summary.get('sha1_as_published')} and "
                                              f"{summary.get('statements')} statements.",
                             sha1_as_published=summary.get("sha1_as_published"))]
            return [note("dump_republished", f"{label}: the dump was republished with another SHA-1.",
                         before=prior.get("sha1_as_published"), after=summary.get("sha1_as_published"),
                         statements_before=prior.get("statements"), statements_after=summary.get("statements"))]
        if old is None:
            return [note("ad_published", f"{label}: ad by {summary.get('advertiser_as_declared')} delivered from "
                                         f"{summary.get('delivery_start')}; ranges as published.",
                         spend_range_as_published=summary.get("spend_range_as_published"),
                         impressions_range_as_published=summary.get("impressions_range_as_published"))]
        if summary.get("listing_state") == "not-returned" and prior.get("listing_state") != "not-returned":
            return [note("ad_not_returned", f"{label}: a complete listing of its declared unit no longer returned "
                                            "this ad (the platform did not state why).",
                         removal_revision_id=new["revision_id"], basis=summary.get("not_returned_basis"))]
        if prior.get("listing_state") == "not-returned" and summary.get("listing_state") == "listed":
            return [note("ad_relisted", f"{label}: the ad is returned again for its declared unit.")]
        ranges = ("spend_range_as_published", "impressions_range_as_published")
        if any(summary.get(k) != prior.get(k) for k in ranges):
            return [note("ad_ranges_revised", f"{label}: the platform now publishes other ranges.",
                         before={k: prior.get(k) for k in ranges}, after={k: summary.get(k) for k in ranges})]
        return [note("ad_revised", f"{label}: revision {new['revision_no']} recorded.",
                     before={k: v for k, v in prior.items() if summary.get(k) != v},
                     after={k: v for k, v in summary.items() if prior.get(k) != v})]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = ""
             ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    def refresh(self, source: Mapping[str, Any], *, run_id: str, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, secret: str | None = None
                ) -> dict[str, Any]:
        """Re-read one declared selection within its budget; idempotent, and every unit leaves a receipt."""
        from src.ingestion.platform_transparency_sources import (
            PlatformTransparencyAdapter,
        )
        from src.kb.platform_transparency_records import PlatformTransparencyProjector

        scopes = set(scopes)
        namespace = PlatformTransparencyProjector._namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = PlatformTransparencyAdapter(source, transport=transport, secret=secret)
        counts: dict[str, int] = {}
        cursor, units = None, 0
        while units < min(len(adapter.units), int(source["budgets"]["max_pages"])):
            page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                       "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            result = self.store.project(namespace, [r["platform_transparency_record"] for r in page.records],
                                        run_id=run_id, source_id=source["source_id"], receipt=dict(page.receipt or {}))
            for change, count in result["counts"].items():
                counts[change] = counts.get(change, 0) + count
            units += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        del principal_id
        return {"run_id": run_id, "source_id": source["source_id"], "units": units, "counts": counts,
                "complete": cursor is None,
                "receipts": self.store.receipts(namespace, run_id, scopes=scopes | {READ_SCOPE})}
