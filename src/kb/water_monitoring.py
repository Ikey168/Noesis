"""Monitor stations, rivers and water bodies through ``platform.subscriptions`` (#2582, WA10 #2632).

A water monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), as the biodiversity and
campaign-finance monitors are (:mod:`src.kb.biodiversity_monitoring`,
:mod:`src.kb.campaign_finance_monitoring`): no monitor table, queue or
scheduler of its own. The query names what is watched - **stations** (id,
number or name), **rivers** (river places; stations join through *accepted*
WA06 matches at each evaluation) and **water bodies** (EU codes). The
``climate-environment-water`` source pack's schedule refreshes the bounded
selections under their budgets, with receipts.

Every stored revision is one cumulative item; the subscription store turns new
items into delivered events, so unchanged data emits nothing and a replay at
the same watermark delivers nothing. Notices are record changes, never
assessments or forecasts:

* ``observation_above_threshold`` - a new observation above a characteristic
  value the gauge operator publishes (the threshold is cited as published);
* ``observation_revised`` / ``observation_removed`` - a value, quality state or
  qualifier changed (e.g. provisional to approved), or the source withdrew it;
* ``station_revised`` - a station's location, gauge zero, datum or thresholds
  changed;
* ``assessment_new_cycle`` / ``assessment_revised`` - a new reporting cycle for
  a water body, or a corrected row of a cycle.

Each notice cites the prior and the new revision and names the fields that
changed. A monitor evaluates only at a committed source-pack watermark whose
run completed every water source it ran.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.water_records import (
    NOTIFICATION_CONTRACT,
    READ_SCOPE,
    SOURCE_PACK,
    WaterError,
    authorize,
)
from src.kb.water_store import WaterStore, table_exists

KIND = "water-monitor"
EVENT_KINDS = ("observation_above_threshold", "observation_revised", "observation_removed", "station_revised",
               "assessment_new_cycle", "assessment_revised")
GENERATION_SPAN = 1_000_000_000
# Published high-water characteristic values (PEGELONLINE: mean high water, highest navigable level, highest known).
DEFAULT_THRESHOLDS = ("MHW", "HSW", "HHW")


def changed_fields(prior: Mapping[str, Any] | None, new: Mapping[str, Any]) -> list[str]:
    if prior is None:
        return sorted(new)
    return sorted(k for k in set(prior) | set(new) if prior.get(k) != new.get(k))


class WaterMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.subscriptions import SubscriptionStore
        from src.kb.water_identity import WaterIdentity
        from src.kb.water_links import WaterLinks

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = WaterStore(conn, initialize=initialize, now=self.now)
        self.identity = WaterIdentity(conn, initialize=initialize, now=self.now)
        self.links = WaterLinks(conn, initialize=initialize, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, principal_id: str, scopes: Iterable[str],
               stations: list[str] | None = None, rivers: list[str] | None = None,
               water_bodies: list[str] | None = None, thresholds: list[str] | None = None,
               delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        """Watch stations (id, number or name), river place ids or water bodies (EU code or name).

        ``thresholds`` names which published characteristic values count as thresholds (default: the high-water
        values MHW, HSW and HHW); a value is never derived by Noesis."""
        from src.kb.water_identity import place_view

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        if not (stations or rivers or water_bodies):
            raise WaterError("invalid_watch", "watch at least one station, river or water body")
        for station in stations or []:
            if not [k for k in self.identity.find(namespace, station) if not k.startswith("wfd:")]:
                raise WaterError("not_found", f"{station!r} reaches no acquired station")
        pending = []
        for body in water_bodies or []:
            if not [k for k in self.identity.find(namespace, body) if k.startswith("wfd:")]:
                # An EU code not yet reported may be watched for its first cycle; anything else must resolve.
                if not re.fullmatch(r"[A-Z]{2}[A-Za-z0-9_.-]{1,60}", str(body)):
                    raise WaterError("not_found", f"{body!r} reaches no acquired water body")
                pending.append(body)
        for river in rivers or []:
            if place_view(self.conn, namespace, river) is None:
                raise WaterError("not_found", f"river place {river!r} is not visible")
        watch = {"stations": sorted(set(stations or [])), "rivers": sorted(set(rivers or [])),
                 "water_bodies": sorted(set(water_bodies or [])),
                 "thresholds": sorted(set(thresholds or DEFAULT_THRESHOLDS))}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "environment",
             "query": {"operation": "search", "kind": KIND, "watch": watch},
             "filters": {"watch": ["stations", "rivers", "water_bodies"]},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "water-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "pending_water_bodies": pending,
                "refresh": f"evaluated at complete {SOURCE_PACK} source-pack runs within the declared bounds and "
                           "provider limits; no separate scheduler"}

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

    def generation(self, namespace: str) -> int:
        reviewed = 0
        if table_exists(self.conn, "water_identity_matches"):
            reviewed = int(self.conn.execute("SELECT count(*) FROM water_identity_matches WHERE namespace=? AND "
                                             "state IN ('accepted', 'reverted', 'rejected')",
                                             [namespace]).fetchone()[0])
        return self.links.generation(namespace) + reviewed

    # ------------------------------------------------------------------ snapshot

    @staticmethod
    def _cite(record: Mapping[str, Any], revision: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if revision is None:
            return None
        return {"record_id": record["record_id"], "revision_id": revision["revision_id"], "event": revision["event"],
                "retrieved_at": revision["retrieved_at"], "url": revision["statement"]["source"]["url"]}

    def _watched_stations(self, namespace: str, watch: Mapping[str, Any]) -> dict[str, list[str]]:
        """Station subject key -> the watch entries that reach it (rivers through accepted matches only)."""
        reach: dict[str, list[str]] = {}
        for station in watch.get("stations") or []:
            for key in self.identity.find(namespace, station):
                if not key.startswith("wfd:"):
                    reach.setdefault(key, []).append(f"station:{station}")
        for river in watch.get("rivers") or []:
            for match in self.identity.accepted(namespace, place_id=river):
                if match["subject_kind"] == "station" and match["relation"] == "on-river":
                    reach.setdefault(match["subject_key"], []).append(f"river:{river}")
        return reach

    def snapshot(self, subscription: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
        namespace = subscription["namespace"]
        watch = subscription["query"]["watch"]
        stations = self._watched_stations(namespace, watch)
        bodies: dict[str, list[str]] = {}
        for body in watch.get("water_bodies") or []:
            for key in self.identity.find(namespace, body):
                if key.startswith("wfd:"):
                    bodies.setdefault(key, []).append(f"water_body:{body}")
        names = set(watch.get("thresholds") or DEFAULT_THRESHOLDS)
        thresholds = {}
        for key in stations:
            record = next((r for r in self.store.records(namespace, record_type="station") if r["subject_key"] == key),
                          None)
            current = self.store.current(namespace, record["record_id"], cutoff_seq=cutoff) if record else None
            thresholds[key] = (current["statement"]["as_published"].get("thresholds") or []) if current else []
        items = []

        def add(targets, kind, record, prior, new, **extra):
            for watched in targets:
                items.append({"id": f"{watched}:{kind}:{new['revision_id']}", "kind": kind, "watched": watched,
                              "record_key": record["record_key"], "prior": prior, "new": new, **extra})

        for record in self.store.records(namespace):
            targets = stations.get(record["subject_key"]) or bodies.get(record["subject_key"])
            if not targets:
                continue
            revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff)
            for index, revision in enumerate(revisions):
                prior = revisions[index - 1] if index else None
                published = revision["statement"]["as_published"]
                before = prior["statement"]["as_published"] if prior else None
                cite_prior, cite_new = self._cite(record, prior), self._cite(record, revision)
                if record["record_type"] == "observation":
                    if revision["event"] == "removed":
                        add(targets, "observation_removed", record, cite_prior, cite_new,
                            what_changed=["withdrawn by the source"], time=published["time"])
                    elif prior is not None:
                        add(targets, "observation_revised", record, cite_prior, cite_new,
                            what_changed=changed_fields(before, published), time=published["time"],
                            value=published["value"], quality=published["quality"]["state"],
                            prior_value=before["value"], prior_quality=before["quality"]["state"])
                    else:
                        exceeded = [t for t in thresholds.get(record["subject_key"], [])
                                    if t["name"] in names and t["parameter"] == published["parameter"]
                                    and t["unit"] == published["unit"]
                                    and t.get("value") is not None and published["value"] > t["value"]]
                        if exceeded:
                            add(targets, "observation_above_threshold", record, None, cite_new,
                                what_changed=["new observation"], time=published["time"], value=published["value"],
                                unit=published["unit"], quality=published["quality"]["state"],
                                thresholds=[{k: t.get(k) for k in ("name", "label", "value", "unit", "valid_from",
                                                                   "basis")} for t in exceeded])
                elif record["record_type"] == "station" and prior is not None:
                    add(targets, "station_revised", record, cite_prior, cite_new,
                        what_changed=changed_fields(before, published))
                elif record["record_type"] == "assessment":
                    add(targets, "assessment_revised" if prior else "assessment_new_cycle", record, cite_prior,
                        cite_new, what_changed=changed_fields(before, published),
                        reporting_cycle=published["cycle_year"])
        return {"items": sorted(items, key=lambda i: i["id"]), "coverage": {"complete": True}}

    # ------------------------------------------------------------------ run

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        mark = self.complete_watermark(namespace, watermark)
        generation = self.generation(namespace)
        combined = mark["source_watermark"] * GENERATION_SPAN + generation
        self.subscriptions.commit_watermark(
            namespace, combined, kind="ingestion",
            detail={"source_pack": SOURCE_PACK, "source_watermark": mark["source_watermark"],
                    "link_identity_generation": generation, "cutoff_seq": mark["cutoff_seq"]},
            committed_at_ms=mark["committed_at_ms"])
        baseline = subscription["last_watermark"] is None
        evaluated = self.subscriptions.evaluate(subscription_id, combined,
                                                self.snapshot(subscription, mark["cutoff_seq"]),
                                                principal_id=principal_id, scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            event_type, key, after = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id]).fetchone()
            if event_type != "added" or not after:
                continue  # items are cumulative per revision; only additions are news
            item = json.loads(after)
            notifications.append({"contract": NOTIFICATION_CONTRACT, "notification_id": f"{event_id}:{item['kind']}",
                                  "event_id": event_id, "object": key, "baseline": baseline,
                                  **{k: v for k, v in item.items() if k != "id"},
                                  "notice": "a record change as published; not an assessment or forecast"})
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


__all__ = ["DEFAULT_THRESHOLDS", "EVENT_KINDS", "KIND", "WaterMonitor", "changed_fields"]
