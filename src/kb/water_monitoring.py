"""Monitor stations, rivers and water bodies through ``platform.subscriptions`` (#2582, WA10 #2632).

A water monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), as the Climate and
Environment and biodiversity monitors are: no monitor table, queue or
scheduler of its own. The query names what is watched - **stations**
(subject key, native id or number), **rivers** (a river place id, resolved at
each evaluation through accepted ``on-river`` matches and the published river
identifier, or a published PEGELONLINE water shortname such as ``ELBE``) and
**water bodies** (EU codes). The ``climate-environment-water`` source pack's
schedule refreshes the bounded selections with receipts.

Every stored revision is one cumulative item; the subscription store turns new
items into delivered events, so unchanged data emits nothing and a replay at
the same watermark delivers nothing:

* ``observation_above_threshold`` - a new or revised observation whose value is
  above a characteristic value the station itself publishes for that series
  (by default its ``MHW`` and ``HSW`` marks; a watch may name others). This is
  a comparison of two published numbers, not a flood warning or assessment;
* ``observation_revised`` - e.g. provisional replaced by approved, or a
  corrected value;
* ``station_revised`` / ``station_removed`` - location, gauge zero or datum,
  thresholds, or withdrawal;
* ``assessment_new_cycle`` / ``assessment_revised`` / ``assessment_removed``.

Each notice cites the new (and prior) revision and states what changed. A
monitor evaluates only at a committed source-pack watermark whose run completed
every water source it ran.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from src.kb.water_records import (
    NOTIFICATION_CONTRACT,
    READ_SCOPE,
    SOURCE_PACK,
    WaterError,
    authorize,
    minimise,
    water_body_subject,
)
from src.kb.water_store import WaterStore, cite, table_exists

KIND = "water-monitor"
EVENT_KINDS = ("observation_above_threshold", "observation_revised", "station_revised", "station_removed",
               "assessment_new_cycle", "assessment_revised", "assessment_removed")
DEFAULT_THRESHOLDS = ("MHW", "HSW")
GENERATION_SPAN = 1_000_000_000


class WaterMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.subscriptions import SubscriptionStore
        from src.kb.water_identity import WaterIdentity

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = WaterStore(conn, initialize=initialize, now=self.now)
        self.identity = WaterIdentity(conn, initialize=initialize, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, principal_id: str, scopes: Iterable[str],
               stations: list[str] | None = None, rivers: list[str] | None = None,
               water_bodies: list[str] | None = None, thresholds: list[str] | None = None,
               delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        """Watch stations, rivers (river place id or published water shortname) or EU water-body codes."""
        from src.kb.water_queries import _code, resolve_station

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        if not (stations or rivers or water_bodies):
            raise WaterError("invalid_watch", "watch at least one station, river or water body")
        watch = {"stations": sorted({resolve_station(self.store, namespace, s)["subject_key"] for s in stations or []}),
                 "rivers": sorted(set(rivers or [])),
                 "water_bodies": sorted({_code(self.store, namespace, w) for w in water_bodies or []}),
                 "thresholds": sorted(set(thresholds or DEFAULT_THRESHOLDS))}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "environment",
             "query": {"operation": "search", "kind": KIND, "watch": watch},
             "filters": {"watch": ["stations", "rivers", "water_bodies"]},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "water-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "watch": watch,
                "refresh": f"evaluated at complete {SOURCE_PACK} source-pack runs within the declared bounds; no "
                           "separate scheduler",
                "notice": "notices are record changes and comparisons with published thresholds, not flood warnings "
                          "or assessments"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != KIND:
            raise WaterError("monitor_not_found", "subscription is not a water monitor")
        return subscription

    def complete_watermark(self, namespace: str, watermark: int | None = None) -> dict[str, Any]:
        if not table_exists(self.conn, "source_pack_watermarks") or not table_exists(self.conn, "water_source_runs"):
            raise WaterError("watermark_uncommitted", "no committed water run yet")
        rows = self.conn.execute(
            "SELECT watermark, committed_at_ms, run_id FROM source_pack_watermarks WHERE pack_id=? "
            "AND (? IS NULL OR watermark=?) ORDER BY watermark DESC", [SOURCE_PACK, watermark, watermark]).fetchall()
        for mark, committed_at, run_id in rows:
            runs = self.store.runs(namespace, run_id)
            if not runs:
                continue
            if any(r["status"] != "complete" for r in runs):
                if watermark is not None:
                    raise WaterError("incomplete_run", "that run did not complete every source it ran")
                continue
            return {"source_watermark": int(mark), "committed_at_ms": int(committed_at), "run_id": run_id,
                    "cutoff_seq": max(r["cutoff_seq"] for r in runs)}
        raise WaterError("watermark_uncommitted", "no complete water run is committed yet")

    # ------------------------------------------------------------------ snapshot

    def _river_stations(self, namespace: str, river: str) -> set[str]:
        from src.kb.water_identity import place_view

        place = place_view(self.conn, namespace, river)
        shortname = (place or {}).get("source_ids", {}).get("pegelonline-water") if place else river
        found = {m["subject_key"] for m in self.identity.accepted(namespace, place_id=river)
                 if m["relation"] == "on-river"} if place else set()
        for record in self.store.records(namespace, record_type="station"):
            revision = self.store.current(namespace, record["record_id"])
            published = revision["statement"]["as_published"] if revision else {}
            if shortname and (published.get("river") or {}).get("shortname") == shortname:
                found.add(record["subject_key"])
        return found

    @staticmethod
    def _above(published: Mapping[str, Any], station: Mapping[str, Any] | None, names: set[str]
               ) -> list[dict[str, Any]]:
        if station is None or published.get("value") is None:
            return []
        hits = []
        for threshold in station["statement"]["as_published"].get("thresholds") or []:
            if threshold.get("series") != published["parameter"] or threshold.get("shortname") not in names \
                    or threshold.get("value") is None or threshold.get("unit") != published["unit"]:
                continue
            if Decimal(published["value"]) > Decimal(threshold["value"]):
                hits.append({"shortname": threshold["shortname"], "longname": threshold.get("longname"),
                             "value": threshold["value"], "unit": threshold["unit"],
                             "valid_from": threshold.get("valid_from"),
                             "station_revision_id": station["revision_id"]})
        return hits

    def snapshot(self, subscription: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
        namespace = subscription["namespace"]
        watch = subscription["query"]["watch"]
        names = set(watch.get("thresholds") or DEFAULT_THRESHOLDS)
        stations = {s: [f"station:{s}"] for s in watch.get("stations") or []}
        for river in watch.get("rivers") or []:
            for key in self._river_stations(namespace, river):
                stations.setdefault(key, []).append(f"river:{river}")
        bodies = {water_body_subject(c): f"water-body:{c}" for c in watch.get("water_bodies") or []}
        items = []

        def add(targets, kind, record, prior, new, **extra):
            for target in targets:
                suffix = f":{extra['threshold']['shortname']}" if "threshold" in extra else ""
                items.append({"id": f"{target}:{kind}:{new['revision_id']}{suffix}", "kind": kind, "watched": target,
                              "record_key": record["record_key"], "prior": prior, "new": new, **extra})

        for record in self.store.records(namespace):
            targets = (stations.get(record["subject_key"]) if record["record_type"] in {"station", "observation"}
                       else [bodies[record["subject_key"]]] if record["subject_key"] in bodies else None)
            if not targets:
                continue
            revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff)
            for index, revision in enumerate(revisions):
                prior = cite(record, revisions[index - 1]) if index else None
                new = cite(record, revision)
                published = revision["statement"]["as_published"]
                if record["record_type"] == "observation":
                    station = self.store.station(namespace, record["subject_key"])
                    station_revision = self.store.current(namespace, station["record_id"], cutoff_seq=cutoff) \
                        if station else None
                    if index:
                        add(targets, "observation_revised", record, prior, new, changes=revision["changes"],
                            quality=published["quality"]["state"], value=published.get("value"),
                            time=published["time"])
                    if index == 0 or "value" in revision["changes"]:
                        for hit in self._above(published, station_revision, names):
                            add(targets, "observation_above_threshold", record, prior, new, threshold=hit,
                                value=published["value"], unit=published["unit"], time=published["time"],
                                quality=published["quality"]["state"],
                                statement="the published value is above the station's published characteristic "
                                          "value; not a warning or an assessment")
                elif record["record_type"] == "station" and index:
                    add(targets, "station_removed" if revision["event"] == "removed" else "station_revised", record,
                        prior, new, changes=revision["changes"])
                elif record["record_type"] == "water_body_assessment":
                    kind = ("assessment_removed" if revision["event"] == "removed" else
                            "assessment_revised" if index else "assessment_new_cycle")
                    add(targets, kind, record, prior, new, cycle=published["cycle"], changes=revision["changes"],
                        status_elements=published["status_elements"])
        return {"items": sorted(items, key=lambda i: i["id"]), "coverage": {"complete": True}}

    # ------------------------------------------------------------------ run

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        mark = self.complete_watermark(namespace, watermark)
        generation = self.identity.generation(namespace)
        combined = mark["source_watermark"] * GENERATION_SPAN + generation
        self.subscriptions.commit_watermark(
            namespace, combined, kind="ingestion",
            detail={"source_pack": SOURCE_PACK, "source_watermark": mark["source_watermark"],
                    "identity_generation": generation, "cutoff_seq": mark["cutoff_seq"]},
            committed_at_ms=mark["committed_at_ms"])
        baseline = subscription["last_watermark"] is None
        evaluated = self.subscriptions.evaluate(subscription_id, combined, self.snapshot(subscription,
                                                                                         mark["cutoff_seq"]),
                                                principal_id=principal_id, scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            event_type, key, after = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id]).fetchone()
            if event_type != "added" or not after:
                continue  # items are cumulative per revision; only additions are news
            item = json.loads(after)
            notifications.append(minimise({
                "contract": NOTIFICATION_CONTRACT, "notification_id": f"{event_id}:{item['kind']}",
                "event_id": event_id, "kind": item["kind"], "object": key, "watched": item["watched"],
                "record_key": item["record_key"], "prior": item["prior"], "new": item["new"], "baseline": baseline,
                "what_changed": {k: item[k] for k in ("changes", "threshold", "value", "unit", "time", "quality",
                                                      "cycle", "status_elements", "statement") if k in item}}))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": combined,
                "source_watermark": mark["source_watermark"], "run_id": mark["run_id"], "baseline": baseline,
                "notifications": notifications, "delivery": subscription["delivery"]}

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        if not table_exists(self.conn, "knowledge_subscriptions"):
            raise WaterError("not_ready", "no water monitor exists yet")
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)


__all__ = ["DEFAULT_THRESHOLDS", "EVENT_KINDS", "KIND", "WaterMonitor"]
