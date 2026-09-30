"""Monitor new reports, dataset revisions and conflict-event releases for a place or crisis (HR11, #2278).

A monitor is a knowledge subscription in :class:`src.kb.subscriptions.SubscriptionStore`
(``platform.subscriptions``); there is no scheduler here. Its evaluated view is
the place or crisis answer (:meth:`HumanitarianQueries.published_about` over
the latest revisions) plus, for an admin place or a bounding box, the conflict
events of a declared bounded window. Each item carries the record revision it
came from, so a new report, a revised dataset or a new release of an event
shows up as a subscription event, and each notice cites the new or changed
revision and names the fields that changed.

Runs evaluate a committed watermark: by default the newest retrieval time of
the namespace's revisions, reused for the same store generation, so a repeated
run is idempotent and produces no new notices. Refreshes themselves are the
bounded, receipted source-pack runs of the ``humanitarian-response`` pack.
Notices are record changes, never alerts, warnings or assessments.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.humanitarian_queries import HumanitarianQueries
from src.kb.humanitarian_records import READ_SCOPE, WRITE_SCOPE, HumanitarianError, canonical, digest, iso
from src.kb.humanitarian_store import authorize, table_exists

CONTRACT = "noesis-humanitarian-notification-v1"
WATCHES = ("reports", "datasets", "events")
_REPORT_TYPES = ("situation_report", "appeal", "crisis")
_DDL = """
CREATE TABLE IF NOT EXISTS humanitarian_monitors(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, target_json TEXT NOT NULL,
 watch_json TEXT NOT NULL, events_json TEXT);
"""


class HumanitarianMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.humanitarian_identity import HumanitarianIdentity
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        identity = HumanitarianIdentity(conn, initialize=initialize, now=now)
        self.store = identity.store
        self.now = self.store.now
        self.queries = HumanitarianQueries(conn, now=now)
        if initialize:
            conn.execute(_DDL)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, principal_id: str, scopes: Iterable[str],
               pcode: str | None = None, place_id: str | None = None, crisis_key: str | None = None,
               watch: Iterable[str] = WATCHES, events: Mapping[str, Any] | None = None,
               delivery: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Subscribe to a place or crisis; ``events`` declares the bounded area and window for conflict events."""

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        watch = sorted(set(watch))
        if not watch or set(watch) - set(WATCHES):
            raise HumanitarianError("invalid_watch", f"watch is a non-empty subset of {WATCHES}")
        if bool(pcode or place_id) == bool(crisis_key):
            raise HumanitarianError("invalid_request", "monitor one place (pcode or place_id) or one crisis")
        target = {"pcode": pcode, "place_id": place_id, "crisis_key": crisis_key}
        events_spec = None
        if "events" in watch:
            spec = dict(events or {})
            area = spec.get("area") or ({"pcode": pcode} if pcode else {"place_id": place_id} if place_id else None)
            if not area or not spec.get("start") or not spec.get("end"):
                raise HumanitarianError("invalid_request", "an events watch declares a bounded area and window")
            events_spec = {"area": area, "start": spec["start"], "end": spec["end"],
                           "imprecise": spec.get("imprecise", "admin")}
            # Validate the bounds once, at creation.
            self.queries.conflict_events(namespace, scopes=scopes, **events_spec)
        created = self.subscriptions.create({
            "namespace": namespace, "domain": "humanitarian",
            "query": {"operation": "search", "kind": "humanitarian-monitor", "target": target, "watch": watch,
                      "events": events_spec},
            "filters": {k: v for k, v in target.items() if v}, "cadence": {"trigger": "watermark"},
            "delivery": dict(delivery or {"kind": "poll"}),
        }, "humanitarian-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        self.conn.execute("INSERT INTO humanitarian_monitors VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [created["subscription_id"], namespace, principal_id, canonical(target), canonical(watch),
                           canonical(events_spec) if events_spec else None])
        return {**created, "watch": watch, "events": events_spec,
                "refresh": "bounded source-pack runs of humanitarian-response; this monitor adds no scheduler"}

    def _monitor(self, subscription_id: str, principal_id: str) -> dict[str, Any]:
        row = None
        if table_exists(self.conn, "humanitarian_monitors"):
            row = self.conn.execute("SELECT namespace, owner, target_json, watch_json, events_json FROM "
                                    "humanitarian_monitors WHERE subscription_id=?", [subscription_id]).fetchone()
        if not row or row[1] != principal_id:
            raise HumanitarianError("monitor_not_found", "humanitarian monitor is unavailable")
        return {"namespace": row[0], "target": json.loads(row[2]), "watch": json.loads(row[3]),
                "events": json.loads(row[4]) if row[4] else None}

    def snapshot(self, monitor: Mapping[str, Any], *, scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes) | {READ_SCOPE}
        namespace, target = monitor["namespace"], monitor["target"]
        answer = self.queries.published_about(namespace, as_of=None, scopes=scopes,
                                              **{k: v for k, v in target.items() if v})
        items = []
        for item in answer["items"]:
            kind = "dataset" if item["record_type"] == "dataset" else "report"
            if (kind == "dataset" and "datasets" not in monitor["watch"]) or \
                    (kind == "report" and "reports" not in monitor["watch"]):
                continue
            items.append({"id": item["record_key"], "kind": kind, "record_type": item["record_type"],
                          "title": item.get("title") or item.get("name"),
                          "revision_id": item["revision_used"]["revision_id"],
                          "revision": item["revision_used"]["revision"], "citation": item["citation"]})
        coverage = {"complete": not any(s["stale"] for s in answer["sources_consulted"] if s["last_success_ms"]),
                    "stale_sources": sorted(s["source"] for s in answer["sources_consulted"]
                                            if s["stale"] and s["last_success_ms"])}
        if monitor.get("events"):
            events = self.queries.conflict_events(namespace, scopes=scopes, **monitor["events"])
            for coder, rows in events["by_coder"].items():
                for event in rows:
                    items.append({"id": event["record_key"], "kind": "event", "coder": coder,
                                  "title": f"{event['coding_source']} event {event['record_key'].rsplit(':', 1)[-1]}",
                                  "revision_id": event["revision_used"]["revision_id"],
                                  "revision": event["revision_used"]["revision"], "coding_status": event["coding_status"],
                                  "dataset_version": event["dataset_version"], "citation": event["citation"]})
        return {"items": items, "coverage": coverage}

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        """Newest retrieval time; reused while the store generation (its revision set) is unchanged."""

        if not self.store.ready():
            raise HumanitarianError("not_ready", "no humanitarian revision yet; run the source pack first")
        count, latest, newest = self.conn.execute(
            "SELECT count(*), max(retrieved_at_ms), max(revision_id) FROM humanitarian_revisions WHERE namespace=?",
            [namespace]).fetchone()
        if not count:
            raise HumanitarianError("not_ready", "no humanitarian revision yet; run the source pack first")
        generation = digest([int(count), newest])[:16]
        rows = self.conn.execute("SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? "
                                 "ORDER BY watermark", [namespace]).fetchall()
        for watermark, detail in reversed(rows):
            if json.loads(detail or "{}").get("humanitarian_generation") == generation:
                return int(watermark), json.loads(detail)
        highest = int(rows[-1][0]) if rows else 0
        return max(int(latest), highest + 1), {"humanitarian_generation": generation, "retrieved_at": iso(int(latest))}

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        monitor = self._monitor(subscription_id, principal_id)
        namespace = monitor["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            watermark, detail = self.default_watermark(namespace)
        else:
            detail = {"declared": True}
        result = self.snapshot(monitor, scopes=scopes)
        self.subscriptions.commit_watermark(namespace, int(watermark), kind="ingestion", detail=detail)
        evaluated = self.subscriptions.evaluate(subscription_id, int(watermark), result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute("SELECT event_type, object_key, before_json, after_json FROM "
                                    "knowledge_subscription_events WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(self._classify(namespace, event_id, *row, scopes=scopes))
        receipts = self.store.receipts(namespace, scopes=scopes)
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": int(watermark),
                "coverage": result["coverage"], "notifications": notifications,
                "refresh_receipts": [r["receipt_id"] for r in receipts[-5:]],
                "notice": "notices are record changes with citations; they are not alerts or warnings"}

    def _classify(self, namespace, event_id, event_type, key, before, after, *, scopes):
        before = json.loads(before) if before else None
        after = json.loads(after) if after else None
        item = after or before or {}

        def note(kind, message, revision):
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id, "kind": kind,
                    "record_key": key, "message": message, "cites": revision}

        if event_type == "coverage-degraded":
            return [note("stale_source", "Refresh failed for " + ", ".join(after.get("stale_sources") or []) +
                         "; stored revisions are unchanged and nothing is treated as withdrawn.", {})]
        if event_type == "removed":
            return [note("no_longer_in_view", f"{before.get('title')} left the monitored view (not treated as withdrawn).",
                         before.get("citation") or {})]
        kind = item.get("kind")
        if event_type == "added":
            label = {"report": "new_report", "dataset": "new_dataset", "event": "new_event"}.get(kind, "new_record")
            return [note(label, f"New {item.get('record_type') or kind}: {item.get('title')} (revision {item.get('revision')}).",
                         after["citation"])]
        changed = []
        history = self.store.history(namespace, key, scopes=scopes)
        current = next((h for h in history if h["revision_id"] == after["revision_id"]), None)
        if current is not None:
            changed = [c["field"] for c in current["changed_fields"]]
        label = {"report": "report_revised", "dataset": "dataset_revised", "event": "event_release_changed"}.get(
            kind, "record_revised")
        return [note(label, f"{after.get('title')}: revision {after.get('revision')} replaces {before.get('revision')}; "
                            f"changed fields: {', '.join(changed) or 'none listed'}.",
                     {**after["citation"], "previous_revision_id": before.get("revision_id"), "changed_fields": changed})]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict[str, Any]:
        scopes = set(scopes)
        self._monitor(subscription_id, principal_id)
        if READ_SCOPE not in scopes and "operator" not in scopes:
            raise HumanitarianError("unauthorized", "humanitarian read scope is required")
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)
