"""Record-change notices for a place: new events, parameter revisions, advisories and alerts (NH12, #2360).

A monitor is a knowledge subscription (:class:`~src.kb.subscriptions.SubscriptionStore`)
over a place, point + radius or bbox. Its evaluated result set is the place's
current hazard view — each event, advisory and alert in the monitored area with
the record revision it came from — evaluated only at a *committed* watermark
(the ingestion watermarks source-pack runs and the maintenance orchestrator
commit). There is no scheduler here; replaying a watermark creates no new
notices, and a refresh over unchanged records creates none either.

Notices are **record changes, not warnings**: each cites the new or changed
record revision and states what changed (parameters, status, level, validity).
This is not an emergency alerting channel and gives no safety advice.
"""

from __future__ import annotations

import json

from src.kb import hazards_records as hr
from src.kb.hazards_queries import _alert_match, _area, events_affecting
from src.kb.hazards_records import READ_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.hazards_store import HazardStore, HazardStoreError, authorize, iso, ms

CONTRACT = "noesis-hazard-notice-v1"
WATCHES = ("events", "revisions", "advisories", "alerts")
LOOKBACK_DAYS = 365
LABEL = "record change notice: what an issuing body published or revised; not a warning and not safety advice"
_DDL = """
CREATE TABLE IF NOT EXISTS hazard_monitors(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, area_json TEXT NOT NULL,
 watch_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
"""


class HazardMonitor:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = HazardStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace, request_key, *, principal_id, scopes, place_id=None, point=None, radius_m=None,
               bbox=None, watch=WATCHES, delivery=None):
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not watch or set(watch) - set(WATCHES):
            raise HazardStoreError("invalid_watch", f"watch is a non-empty subset of {WATCHES}")
        area = {"place_id": place_id, "point": point, "radius_m": radius_m, "bbox": bbox}
        _area(self.store, namespace, place_id=place_id, point=point, radius_m=radius_m, bbox=bbox, as_of_ms=None)
        created = self.subscriptions.create({
            "namespace": namespace, "domain": "natural-hazards",
            "query": {"operation": "search", "kind": "hazard-place", "area": area, "watch": sorted(watch)},
            "filters": {k: v for k, v in area.items() if v is not None}, "cadence": {"trigger": "watermark"},
            "delivery": delivery or {"kind": "poll"},
        }, "hazard-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        self.conn.execute("INSERT INTO hazard_monitors VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [created["subscription_id"], namespace, principal_id, canonical(area),
                           canonical(sorted(watch)), self.now()])
        return {**created, "area": area, "watch": sorted(watch), "label": LABEL,
                "refresh": "source-pack runs and the maintenance orchestrator commit the watermarks this monitor evaluates"}

    def _monitor(self, subscription_id, principal_id):
        row = self.conn.execute("SELECT namespace, owner, area_json, watch_json FROM hazard_monitors WHERE subscription_id=?",
                                [subscription_id]).fetchone()
        if not row or row[1] != principal_id:
            raise HazardStoreError("monitor_not_found", "hazard monitor is unavailable")
        return {"namespace": row[0], "area": json.loads(row[2]), "watch": json.loads(row[3])}

    def snapshot(self, monitor, *, scopes, principal_id):
        """The monitored area's current records, one item per record carrying its current revision."""

        namespace, area, watch = monitor["namespace"], monitor["area"], monitor["watch"]
        read = set(scopes) | {READ_SCOPE}
        now = self.now()
        items = []
        if {"events", "revisions"} & set(watch):
            answer = events_affecting(self.conn, namespace, start=iso(now - LOOKBACK_DAYS * 86_400_000), end=iso(now),
                                      scopes=read, principal_id=principal_id, now=self.now, **area)
            for event in answer["events"]:
                used = event["revision_used"]
                items.append({"id": f"event:{event['record_id']}", "kind": "event", "record_id": event["record_id"],
                              "title": event["title"], "provider": event["provider"], "revision_id": used["revision_id"],
                              "revision_key": used["revision_key"], "published_at": used["published_at"],
                              "status": event["status"], "parameters_digest": digest(event["parameters"])})
        located = _area(self.store, namespace, as_of_ms=None, **area)
        for record_type, wanted in (("advisory", "advisories"), ("alert", "alerts")):
            if wanted not in watch:
                continue
            for record_id in self.store.record_ids(namespace, record_type=record_type):
                revision = self.store.revision_at(record_id)
                issued = ms(revision["content"].get("issued_at")) or revision["published_at_ms"]
                if issued < now - LOOKBACK_DAYS * 86_400_000:
                    continue
                if _alert_match(self.store, namespace, located, located.get("iso3"), revision, principal_id) is None:
                    continue
                content = revision["content"]
                items.append({"id": f"{record_type}:{record_id}", "kind": record_type, "record_id": record_id,
                              "title": content["title"], "provider": self.store._header(record_id)["provider"],
                              "revision_id": revision["revision_id"], "revision_key": revision["revision_key"],
                              "issued_at": content.get("issued_at"), "level": content.get("level"),
                              "advisory_number": content.get("advisory_number"), "valid_to": content.get("valid_to"),
                              "status": content.get("status")})
        stale = sorted(p for p in hr.PROVIDERS if self.store.provider_state(namespace, p)["acquired"]
                       and self.store.provider_state(namespace, p)["stale"])
        return {"items": items, "coverage": {"complete": not stale, "stale_providers": stale}}

    def run(self, subscription_id, watermark=None, *, principal_id, scopes):
        """Evaluate at a committed watermark; returns cited record-change notices (idempotent per watermark)."""

        monitor = self._monitor(subscription_id, principal_id)
        namespace = monitor["namespace"]
        if watermark is None:
            row = self.conn.execute("SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                                    [namespace]).fetchone()
            if row is None or row[0] is None:
                raise HazardStoreError("watermark_uncommitted",
                                       "no committed watermark yet; source-pack runs and the maintenance orchestrator commit them")
            watermark = int(row[0])
        result = self.snapshot(monitor, scopes=scopes, principal_id=principal_id)
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notices = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute("SELECT event_type, object_key, before_json, after_json FROM knowledge_subscription_events "
                                    "WHERE event_id=?", [event_id]).fetchone()
            notices.extend(self._classify(namespace, event_id, *row, scopes=scopes))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark,
                "coverage": result["coverage"], "notices": notices, "label": LABEL,
                "receipt": {"result_hash": evaluated.get("result_hash"), "events": evaluated.get("events", 0),
                            "status": evaluated["status"]},
                "delivery": "configured subscription channel (poll by default)"}

    def _classify(self, namespace, event_id, event_type, key, before, after, *, scopes):
        before = json.loads(before) if before else None
        after = json.loads(after) if after else None

        def notice(kind, message, item, changes=None):
            return {"contract": CONTRACT, "notice_id": f"{event_id}:{kind}", "event_id": event_id, "kind": kind,
                    "object": key, "message": message, "label": LABEL, "changes": changes or [],
                    "cites": {"record_id": item.get("record_id"), "revision_id": item.get("revision_id"),
                              "revision_key": item.get("revision_key"), "provider": item.get("provider")}}

        if event_type == "coverage-degraded":
            return [{"contract": CONTRACT, "notice_id": f"{event_id}:stale_source", "event_id": event_id,
                     "kind": "stale_source", "object": key, "label": LABEL, "changes": [], "cites": {},
                     "message": "Refresh failed for " + ", ".join(after.get("stale_providers") or [])
                                + "; records are uncertain, not removed."}]
        if event_type == "removed":
            return [notice("no_longer_in_view", f"{before.get('title')} left the monitored view (not treated as ended).",
                           before)]
        kind = (after or {}).get("kind")
        if event_type == "added":
            label = {"event": "new_event", "advisory": "new_advisory", "alert": "new_alert"}[kind]
            detail = f" advisory {after['advisory_number']}" if after.get("advisory_number") else (
                f" level {after['level']}" if after.get("level") else "")
            return [notice(label, f"{after['title']}{detail} published {after.get('published_at') or after.get('issued_at')} "
                                  f"(revision {after['revision_key']}).", after)]
        changes = self._changes(namespace, after["record_id"], before.get("revision_id"), after["revision_id"], scopes)
        label = "parameter_revision" if kind == "event" else "revised_" + kind
        what = ", ".join(sorted({c["parameter"] for c in changes})) or "record content"
        return [notice(label, f"{after['title']}: {what} revised by the publisher (revision {before.get('revision_key')} "
                              f"-> {after['revision_key']}).", after, changes)]

    def _changes(self, namespace, record_id, before_revision, after_revision, scopes):
        history = self.store.revisions(namespace, record_id, scopes=set(scopes) | {READ_SCOPE})["revisions"]
        by_id = {h["revision_id"]: h for h in history}
        if before_revision in by_id and after_revision in by_id:
            return hr.parameter_changes(by_id[before_revision]["content"], by_id[after_revision]["content"])
        return by_id[after_revision]["changes"] if after_revision in by_id else []

    def poll(self, subscription_id, *, principal_id, scopes, cursor=""):
        self._monitor(subscription_id, principal_id)
        if READ_SCOPE not in scopes and "operator" not in scopes:
            raise HazardStoreError("unauthorized", "hazards read scope is required")
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)
