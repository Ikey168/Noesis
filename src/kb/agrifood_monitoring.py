"""Monitor new releases, revisions and linked food alerts through ``platform.subscriptions`` (#2213, AF10 #2362).

An agri-food monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`) whose query names a commodity
and a place; the commodity codes (with those accepted crosswalks reach) and the
place codes are resolved when the monitor is created and kept in the watch.
There is no monitor table, queue or scheduler of its own: the ``agrifood``
source pack's schedule refreshes the sources, and deliveries go through the
subscription outbox.

Each evaluation lists cumulative items, one per event, and the subscription
store turns new items into delivered events (so a replay delivers nothing):

* ``release`` - a figure first published in a vintage;
* ``revision`` - a later vintage publishing another value or flag for a
  period, with the **old and new** value, flag and citation;
* ``linked_alert`` - a RASFF notice newly linked by explicit citation.

Thresholds are the user's own (``min_abs_change`` / ``min_pct_change`` on
revisions); the pack derives none. A monitor evaluates only at a committed
``agrifood`` watermark whose run completed every source it ran, with only the
values projected up to that run.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from src.kb.agrifood_records import NOTIFICATION_CONTRACT, READ_SCOPE, SOURCE_PACK, AgrifoodError, authorize
from src.kb.agrifood_store import AgrifoodStore, table_exists

KIND = "agrifood-monitor"
EVENT_KINDS = ("release", "revision", "linked_alert")
THRESHOLDS = ("min_abs_change", "min_pct_change")
GENERATION_SPAN = 1_000_000_000


def _thresholds(value: Mapping[str, Any] | None) -> dict[str, str]:
    out = {}
    for key, item in dict(value or {}).items():
        if key not in THRESHOLDS:
            raise AgrifoodError("invalid_watch", f"thresholds are user-set {THRESHOLDS}")
        number = Decimal(str(item))
        if number < 0:
            raise AgrifoodError("invalid_watch", "a threshold is not negative")
        out[key] = str(number)
    return out


def _passes(old: Any, new: Any, thresholds: Mapping[str, str]) -> bool:
    if not thresholds or old is None or new is None:
        return True  # a value filled, withheld or re-flagged is always news
    change = abs(Decimal(new) - Decimal(old))
    if "min_abs_change" in thresholds and change < Decimal(thresholds["min_abs_change"]):
        return False
    if "min_pct_change" in thresholds and Decimal(old) != 0:
        return change / abs(Decimal(old)) * 100 >= Decimal(thresholds["min_pct_change"])
    return True


class AgrifoodMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = AgrifoodStore(conn, initialize=initialize, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, commodity: str, place: str, principal_id: str,
               scopes: Iterable[str], measures: list[str] | None = None, thresholds: Mapping[str, Any] | None = None,
               delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        """Watch a commodity and a place (as the series query resolves them) with user-set thresholds only."""
        from src.kb.agrifood_queries import AgrifoodQueries

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        queries = AgrifoodQueries(self.conn, now=self.now)
        answer = queries.series_as_of(namespace, commodity=commodity, place=place, scopes=scopes, measures=measures)
        if not answer["commodity_codes"] or not answer["place"]["codes"]:
            raise AgrifoodError("not_found", "the commodity or place reaches no acquired code")
        watch = {"commodity": commodity, "place": place,
                 "commodity_codes": sorted([c["scheme"], c["code"]] for c in answer["commodity_codes"]),
                 "place_codes": sorted([c["scheme"], c["code"]] for c in answer["place"]["codes"]),
                 "measures": sorted(measures or []), "thresholds": _thresholds(thresholds)}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "agrifood",
             "query": {"operation": "search", "kind": KIND, "watch": watch},
             "filters": {"watch": ["commodity", "place"]},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "agrifood-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "watch": watch,
                "refresh": f"evaluated at complete {SOURCE_PACK} source-pack runs; no separate scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != KIND:
            raise AgrifoodError("monitor_not_found", "subscription is not an agri-food monitor")
        return subscription

    def complete_watermark(self, namespace: str, watermark: int | None = None) -> dict[str, Any]:
        if not table_exists(self.conn, "source_pack_watermarks") or not table_exists(self.conn,
                                                                                      "agrifood_source_runs"):
            raise AgrifoodError("watermark_uncommitted", "no committed agri-food run yet")
        rows = self.conn.execute(
            "SELECT watermark, committed_at_ms, run_id FROM source_pack_watermarks WHERE pack_id=? "
            "AND (? IS NULL OR watermark=?) ORDER BY watermark DESC", [SOURCE_PACK, watermark, watermark]).fetchall()
        for mark, committed_at, run_id in rows:
            runs = self.store.runs(namespace, run_id)
            if not runs:
                continue
            if any(r["status"] != "complete" for r in runs):
                if watermark is not None:
                    raise AgrifoodError("incomplete_run", "that run did not complete every source; it is never "
                                                          "evaluated")
                continue
            return {"source_watermark": int(mark), "committed_at_ms": int(committed_at), "run_id": run_id,
                    "cutoff_seq": max(r["cutoff_seq"] for r in runs)}
        raise AgrifoodError("watermark_uncommitted", "no complete agri-food run is committed yet; partial runs are "
                                                     "never evaluated")

    def generation(self, namespace: str) -> int:
        from src.kb.agrifood_links import AgrifoodLinks

        return AgrifoodLinks(self.conn, initialize=False).generation(namespace)

    @staticmethod
    def _side(series: Mapping[str, Any], vintage: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
        source = value["statement"]["source"]
        return {"value": value["value"], "value_text": value["value_text"], "status": value["status"],
                "flag": value["flag"], "estimate_type": value["estimate_type"],
                "released_at": vintage["released_at"],
                "citation": {"series_id": series["series_id"], "vintage_id": vintage["vintage_id"],
                             "release_key": vintage["release_key"], "url": source["url"],
                             "locator": source["locator"], "attribution": source["attribution"]}}

    def snapshot(self, subscription: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
        from src.kb.agrifood_links import AgrifoodLinks

        namespace = subscription["namespace"]
        watch = subscription["query"]["watch"]
        commodities = {tuple(c) for c in watch["commodity_codes"]}
        places = {tuple(p) for p in watch["place_codes"]}
        thresholds = watch.get("thresholds") or {}
        items: list[dict[str, Any]] = []
        for series in self.store.series_list(namespace, commodities=commodities, places=places):
            if watch.get("measures") and series["measure_kind"] not in watch["measures"]:
                continue
            prior: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
            for vintage in self.store.vintages(namespace, series["series_id"]):
                for value in self.store.values(namespace, vintage["vintage_id"], cutoff_seq=cutoff):
                    key = value["period_key"]
                    before = prior.get(key)
                    new = self._side(series, vintage, value)
                    if before is None:
                        items.append({"id": f"{series['series_id']}:{vintage['vintage_id']}:{key}", "kind": "release",
                                      "series_id": series["series_id"], "provider": series["provider"],
                                      "measure": series["measure_label"], "unit": series["unit"], "period": key,
                                      "prior": None, "new": new})
                    else:
                        old = self._side(series, before[1], before[0])
                        changed = (old["value"], old["flag"]["code"], old["status"]) != (
                            new["value"], new["flag"]["code"], new["status"])
                        if changed and _passes(old["value"], new["value"], thresholds):
                            items.append({"id": f"{series['series_id']}:{vintage['vintage_id']}:{key}",
                                          "kind": "revision", "series_id": series["series_id"],
                                          "provider": series["provider"], "measure": series["measure_label"],
                                          "unit": series["unit"], "period": key, "prior": old, "new": new})
                    prior[key] = (value, vintage)
        links = AgrifoodLinks(self.conn, initialize=False)
        seen = set()
        for link in links.links(namespace, scopes={"operator"}, commodities=commodities, owner="products-rasff"):
            if (link["target_id"], link["target_revision"]) in seen:
                continue  # one alert per notice revision, whichever field named the commodity first
            seen.add((link["target_id"], link["target_revision"]))
            items.append({"id": f"{link['target_id']}:{link['target_revision']}", "kind": "linked_alert", "series_id": None,
                          "provider": "products-rasff", "measure": None, "unit": None, "period": None, "prior": None,
                          "new": {"notice_id": link["target_id"], "notice_revision": link["target_revision"],
                                  "matched": link["matched"], "citing_text": link["citing_text"],
                                  "commodity": link["commodity"], "notice_number": link["locator"].get(
                                      "notice_number")}})
        return {"items": items, "coverage": {"complete": True}}

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        mark = self.complete_watermark(namespace, watermark)
        generation = self.generation(namespace)
        if generation >= GENERATION_SPAN:
            raise AgrifoodError("generation_overflow", "link generation exceeds the watermark span")
        combined = mark["source_watermark"] * GENERATION_SPAN + generation
        self.subscriptions.commit_watermark(
            namespace, combined, kind="ingestion",
            detail={"source_pack": SOURCE_PACK, "source_watermark": mark["source_watermark"],
                    "link_generation": generation, "cutoff_seq": mark["cutoff_seq"]},
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
                continue  # items are cumulative per vintage and link; only additions are news
            item = json.loads(after)
            notifications.append({"contract": NOTIFICATION_CONTRACT, "notification_id": f"{event_id}:{item['kind']}",
                                  "event_id": event_id, "kind": item["kind"], "object": key,
                                  "series_id": item["series_id"], "provider": item["provider"],
                                  "measure": item["measure"], "period": item["period"], "prior": item["prior"],
                                  "new": item["new"], "baseline": baseline, "message": _message(item)})
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": combined,
                "source_watermark": mark["source_watermark"], "run_id": mark["run_id"], "baseline": baseline,
                "notifications": notifications, "delivery": subscription["delivery"]}

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        if not table_exists(self.conn, "knowledge_subscriptions"):
            raise AgrifoodError("not_ready", "no agri-food monitor exists yet")
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)


def _message(item: Mapping[str, Any]) -> str:
    """A plain restatement of what the publisher released; never a forecast or advice."""
    new = item["new"]
    if item["kind"] == "linked_alert":
        return f"RASFF notification {new.get('notice_number')} names this commodity ({new['matched']})."
    shown = new["value_text"] if new["value_text"] is not None else new["status"]
    what = f"{item['provider']} {item['measure']} {item['period']}: {shown} {item['unit'] or ''}".strip()
    if item["kind"] == "release":
        return f"{what} released {new['released_at'] or 'without a release date'}."
    prior = item["prior"]
    before = prior["value_text"] if prior["value_text"] is not None else prior["status"]
    return (f"{what} revised in {new['citation']['release_key']} (was {before}, flag {prior['flag']['code']}, in "
            f"{prior['citation']['release_key']}).")


__all__ = ["EVENT_KINDS", "KIND", "THRESHOLDS", "AgrifoodMonitor"]
