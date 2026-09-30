"""Watch a zone, country or plant for new releases, revisions and capacity changes (EN11).

A monitor is a knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`)
whose evaluated result set is the subject's current energy view: one item per
series with the latest vintage, its values and citation, one item per capacity
series with the capacity in force, and one item per value crossing a
**user-configured** threshold. Evaluation happens only at a committed
watermark (source-pack runs and the maintenance orchestrator commit them), so
there is no scheduler here; replaying a watermark creates no new events and an
unchanged view creates none either (deduplication by the subscription store).

Notifications classify the subscription events:

* ``new_release`` - a series entered the view or a new vintage replaced the
  previous one without changing any previously published period;
* ``revision`` - a later vintage changed values of periods already published,
  shown with the old and new value and both vintage citations;
* ``capacity_change`` - the capacity in force changed (old and new value and
  effective dates, both cited);
* ``threshold_crossed`` - a value crossed a threshold the user configured.

The pack derives no thresholds or alerts of its own.
"""

from __future__ import annotations

import json
from decimal import Decimal

from src.kb.energy_records import READ_SCOPE, WRITE_SCOPE, canonical
from src.kb.energy_store import EnergyStoreError, authorize

CONTRACT = "noesis-energy-notification-v1"
_OPS = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b}
WATCHES = ("releases", "revisions", "capacity", "thresholds")
MAX_VALUES = 2000
_DDL = """
CREATE TABLE IF NOT EXISTS energy_monitors(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, subject TEXT NOT NULL,
 record_types_json TEXT NOT NULL, thresholds_json TEXT NOT NULL, watch_json TEXT NOT NULL);
"""


def _threshold(item, index):
    if not isinstance(item, dict) or item.get("op") not in _OPS or item.get("value") is None:
        raise EnergyStoreError("invalid_threshold", "thresholds need record_type, op (gt/gte/lt/lte), value and unit")
    try:
        value = str(Decimal(str(item["value"])))
    except Exception as exc:
        raise EnergyStoreError("invalid_threshold", "threshold value must be a decimal") from exc
    if not item.get("record_type") or not item.get("unit"):
        raise EnergyStoreError("invalid_threshold", "thresholds name the record type and the published unit they compare")
    return {"threshold_id": f"t{index + 1}", "record_type": item["record_type"], "op": item["op"], "value": value,
            "unit": item["unit"], "provider": item.get("provider"), "fuel": item.get("fuel"),
            "basis": "user-configured", "notice": "user threshold; the pack derives none"}


class EnergyMonitor:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.energy_queries import EnergyQueries
        from src.kb.energy_store import EnergyStore
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = EnergyStore(conn, initialize=initialize, now=now)
        self.queries = EnergyQueries(conn, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace, request_key, *, subject, principal_id, scopes, record_types=None, thresholds=(),
               watch=WATCHES, delivery=None):
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not watch or set(watch) - set(WATCHES):
            raise EnergyStoreError("invalid_watch", f"watch is a non-empty subset of {WATCHES}")
        types = sorted(record_types or ("generation", "load", "price", "capacity", "cross_border_flow", "energy_balance"))
        parsed = [_threshold(item, index) for index, item in enumerate(thresholds or [])]
        created = self.subscriptions.create({
            "namespace": namespace, "domain": "energy",
            "query": {"operation": "search", "kind": "energy-subject", "subject": subject, "record_types": types,
                      "thresholds": parsed, "watch": sorted(watch)},
            "filters": {"subject": subject}, "cadence": {"trigger": "watermark"},
            "delivery": delivery or {"kind": "poll"},
        }, "energy-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        self.conn.execute("INSERT INTO energy_monitors VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [created["subscription_id"], namespace, principal_id, subject, canonical(types),
                           canonical(parsed), canonical(sorted(watch))])
        return {**created, "thresholds": parsed, "watch": sorted(watch),
                "refresh": "source-pack runs and the maintenance orchestrator commit the watermarks this monitor evaluates"}

    def _monitor(self, subscription_id, principal_id):
        row = self.conn.execute("SELECT namespace, owner, subject, record_types_json, thresholds_json, watch_json FROM "
                                "energy_monitors WHERE subscription_id=?", [subscription_id]).fetchone()
        if not row or row[1] != principal_id:
            raise EnergyStoreError("monitor_not_found", "energy monitor is unavailable")
        return {"namespace": row[0], "subject": row[2], "record_types": set(json.loads(row[3])),
                "thresholds": json.loads(row[4]), "watch": json.loads(row[5])}

    def snapshot(self, monitor, *, scopes):
        namespace = monitor["namespace"]
        read = set(scopes) | {READ_SCOPE}
        answer = self.queries.observations(namespace, monitor["subject"], monitor["record_types"], scopes=read)
        items, stale = [], set()
        for source in answer["sources"]:
            for series in source["series"]:
                if series.get("status") == "not_published_by_as_of":
                    continue
                if self.store.provider_state(namespace, series["provider"])["stale"]:
                    stale.add(series["provider"])
                values = {v["start"]: v["value"] for v in series["values"][:MAX_VALUES]}
                if {"releases", "revisions"} & set(monitor["watch"]):
                    items.append({"id": f"series:{series['series_id']}", "kind": "series", "title": series["title"],
                                  "series_id": series["series_id"], "vintage_id": series["vintage_id"],
                                  "status": series["publication_status"], "release": series["citation"]["release"],
                                  "values": values, "unit": series["unit"], "citation": series["citation"]})
                if "capacity" in monitor["watch"] and series["record_type"] == "capacity":
                    latest = series["values"][-1] if series["values"] else None
                    record = self.store.vintage(namespace, series["vintage_id"], scopes=read)["record"]
                    items.append({"id": f"capacity:{series['series_id']}", "kind": "capacity", "title": series["title"],
                                  "series_id": series["series_id"], "vintage_id": series["vintage_id"],
                                  "value": None if latest is None else latest["value"],
                                  "period": None if latest is None else latest["start"], "unit": series["unit"],
                                  "effective_from": (record["capacity"] or {}).get("effective_from"),
                                  "effective_to": (record["capacity"] or {}).get("effective_to"),
                                  "operating_status": (record["capacity"] or {}).get("operating_status"),
                                  "citation": series["citation"]})
                if "thresholds" in monitor["watch"]:
                    for threshold in monitor["thresholds"]:
                        if threshold["record_type"] != series["record_type"] or threshold["unit"] != series["unit"]:
                            continue
                        if threshold["provider"] and threshold["provider"] != series["provider"]:
                            continue
                        fuel = (series["facets"].get("fuel") or {}).get("code")
                        if threshold["fuel"] and threshold["fuel"] != fuel:
                            continue
                        for value in series["values"]:
                            if value["value"] is None:
                                continue
                            if _OPS[threshold["op"]](Decimal(value["value"]), Decimal(threshold["value"])):
                                items.append({"id": f"threshold:{threshold['threshold_id']}:{series['series_id']}:{value['start']}",
                                              "kind": "threshold", "title": series["title"], "period_start": value["start"],
                                              "value": value["value"], "unit": series["unit"], "threshold": threshold,
                                              "vintage_id": series["vintage_id"], "citation": series["citation"]})
        return {"items": items, "coverage": {"complete": not stale, "stale_providers": sorted(stale),
                                             "status": answer["status"]}}

    def run(self, subscription_id, watermark=None, *, principal_id, scopes):
        monitor = self._monitor(subscription_id, principal_id)
        namespace = monitor["namespace"]
        if watermark is None:
            row = self.conn.execute("SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                                    [namespace]).fetchone()
            if row is None or row[0] is None:
                raise EnergyStoreError("watermark_uncommitted",
                                       "no committed watermark yet; source-pack runs and the maintenance orchestrator commit them")
            watermark = int(row[0])
        result = self.snapshot(monitor, scopes=scopes)
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute("SELECT event_type, object_key, before_json, after_json FROM knowledge_subscription_events "
                                    "WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark,
                "coverage": result["coverage"], "notifications": notifications,
                "delivery": "configured subscription channel (poll by default)"}

    @staticmethod
    def _classify(event_id, event_type, key, before, after):
        before = json.loads(before) if before else None
        after = json.loads(after) if after else None
        item = after or before or {}

        def note(kind, message, cites, **extra):
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id, "kind": kind,
                    "object": key, "message": message, "cites": cites, **extra}

        if event_type == "coverage-degraded":
            return [note("stale_source", "Refresh failed or never succeeded for " + ", ".join(after.get("stale_providers") or [])
                         + "; affected series are uncertain, not removed.", {})]
        if event_type == "removed":
            return [note("no_longer_in_view", f"{before.get('title')} left the monitored view (not treated as withdrawn).",
                         {"vintage_id": before.get("vintage_id")})]
        kind = item.get("kind")
        if kind == "series":
            if event_type == "added":
                return [note("new_release", f"{after['title']}: release {after['release']['key']} ({after['status']}).",
                             {"vintage": after["citation"]})]
            if before["vintage_id"] == after["vintage_id"]:
                return []
            changed = [{"period_start": start, "old": before["values"][start], "new": after["values"].get(start)}
                       for start in sorted(before["values"]) if start in after["values"]
                       and before["values"][start] != after["values"][start]]
            if changed:
                return [note("revision", f"{after['title']}: {len(changed)} published figure(s) revised by release "
                                         f"{after['release']['key']} ({after['status']}).",
                             {"old_vintage": before["citation"], "new_vintage": after["citation"]}, changes=changed,
                             unit=after["unit"])]
            return [note("new_release", f"{after['title']}: new release {after['release']['key']} ({after['status']}); "
                                        "no previously published figure changed.",
                         {"old_vintage": before["citation"], "new_vintage": after["citation"]})]
        if kind == "capacity":
            if event_type == "added":
                return []
            if (before["value"], before["effective_to"], before["operating_status"]) == (
                    after["value"], after["effective_to"], after["operating_status"]):
                return []
            return [note("capacity_change", f"{after['title']}: {before['value']} → {after['value']} {after['unit']} "
                                            f"(status {before['operating_status']} → {after['operating_status']}).",
                         {"old_vintage": before["citation"], "new_vintage": after["citation"]},
                         old={k: before[k] for k in ("value", "period", "effective_from", "effective_to", "operating_status")},
                         new={k: after[k] for k in ("value", "period", "effective_from", "effective_to", "operating_status")})]
        if kind == "threshold" and event_type == "added":
            threshold = after["threshold"]
            return [note("threshold_crossed", f"{after['title']}: {after['value']} {after['unit']} at {after['period_start']} "
                                              f"{threshold['op']} {threshold['value']} ({threshold['notice']}).",
                         {"vintage": after["citation"], "threshold_basis": threshold["basis"]})]
        return []

    def poll(self, subscription_id, *, principal_id, scopes, cursor=""):
        self._monitor(subscription_id, principal_id)
        if READ_SCOPE not in scopes and "operator" not in scopes:
            raise EnergyStoreError("unauthorized", "energy read scope is required")
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)
