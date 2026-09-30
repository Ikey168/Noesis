"""Monitor authorisations, IUU listings and statistics releases through ``platform.subscriptions`` (#2222, FI11 #2337).

A fisheries monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`) whose query names what is
watched: vessels (provider subject keys, plus the records an accepted identity
match joins to them), lists (``iccat:authorised-vessels``,
``combined-iuu:iuu-vessels``, ...), areas (published area codes) and species
(ASFIS codes). There is no monitor table, queue or scheduler of its own: the
``fisheries-maritime`` source pack's schedule refreshes the sources within the
FI01 bounds, with receipts, and deliveries go through the subscription outbox.

Change detection compares list snapshots and releases through the store's
revisions: every revision is one cumulative item, and the subscription store
turns new items into delivered events:

* ``authorisation_new`` / ``authorisation_amended`` / ``authorisation_removed``;
* ``iuu_listing`` / ``iuu_delisting`` / ``iuu_list_removal``;
* ``statistics_release`` - a catch observation or effort aggregate in a
  watched area or species published by a new release or dataset version.

Every event is dated and cites the prior and the new revision; no advisory or
enforcement text is added. A monitor evaluates only at a committed source-pack
watermark whose run completed every fisheries source it ran, with the
revisions projected up to that run, so replays never deliver duplicates.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.fisheries_records import READ_SCOPE, SOURCE_PACK, FisheriesError, authorize
from src.kb.fisheries_store import FisheriesStore, table_exists

CONTRACT = "noesis-fisheries-notification-v1"
KIND = "fisheries-monitor"
EVENT_KINDS = ("authorisation_new", "authorisation_amended", "authorisation_removed", "iuu_listing",
               "iuu_delisting", "iuu_list_removal", "statistics_release")
GENERATION_SPAN = 1_000_000_000


def _kind(record_type: str, event: str, index: int) -> str:
    if record_type == "authorisation":
        return "authorisation_removed" if event == "removed" else "authorisation_new" if index == 0 \
            else "authorisation_amended"
    if record_type == "listing":
        return {"removed": "iuu_list_removal", "delisted": "iuu_delisting"}.get(event, "iuu_listing")
    return "statistics_release"


class FisheriesMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.fisheries_identity import FisheriesIdentity
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = FisheriesStore(conn, initialize=initialize, now=self.now)
        self.identity = FisheriesIdentity(conn, initialize=initialize, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    # ------------------------------------------------------------------ create

    def create(self, namespace: str, request_key: str, *, principal_id: str, scopes: Iterable[str],
               vessels: list[str] | None = None, lists: list[str] | None = None, areas: list[str] | None = None,
               species: list[str] | None = None, delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        """Watch vessels (identifier or subject key), lists, area codes or ASFIS species."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        if not (vessels or lists or areas or species):
            raise FisheriesError("invalid_watch", "watch at least one vessel, list, area or species")
        subjects = set()
        for entry in vessels or []:
            _, found = self.identity.find(namespace, entry)
            if not found:
                raise FisheriesError("not_found", f"{entry!r} reaches no acquired vessel record")
            subjects |= {min(self.identity.members(namespace, key)) for key in found}
        known_lists = {r["list_key"] for r in self.store.records(namespace) if r["list_key"]}
        unknown = sorted(set(lists or []) - known_lists)
        if unknown:
            raise FisheriesError("not_found", f"no acquired list {unknown[0]!r}")
        watch = {"vessels": sorted(subjects), "lists": sorted(set(lists or [])),
                 "areas": sorted({str(a) for a in areas or []}), "species": sorted({s.upper() for s in species or []}),
                 "requested": {"vessels": sorted(set(vessels or []))}}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "fisheries",
             "query": {"operation": "search", "kind": KIND, "watch": watch},
             "filters": {"watch": ["vessels", "lists", "areas", "species"]},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "fisheries-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": f"evaluated at complete {SOURCE_PACK} source-pack runs within the declared "
                                      "bounds; no separate scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != KIND:
            raise FisheriesError("monitor_not_found", "subscription is not a fisheries monitor")
        return subscription

    # ------------------------------------------------------------------ watermark

    def complete_watermark(self, namespace: str, watermark: int | None = None) -> dict[str, Any]:
        if not table_exists(self.conn, "source_pack_watermarks") or not table_exists(self.conn,
                                                                                      "fisheries_source_runs"):
            raise FisheriesError("watermark_uncommitted", "no committed fisheries run yet")
        rows = self.conn.execute(
            "SELECT watermark, committed_at_ms, run_id FROM source_pack_watermarks WHERE pack_id=? "
            "AND (? IS NULL OR watermark=?) ORDER BY watermark DESC", [SOURCE_PACK, watermark, watermark]).fetchall()
        for mark, committed_at, run_id in rows:
            runs = self.store.runs(namespace, run_id)
            if not runs:
                continue
            if any(r["status"] != "complete" for r in runs):
                if watermark is not None:
                    raise FisheriesError("incomplete_run", "that run did not complete every fisheries source it ran; "
                                                           "it is never evaluated",
                                         failed=sorted(r["source_id"] for r in runs if r["status"] != "complete"))
                continue
            return {"source_watermark": int(mark), "committed_at_ms": int(committed_at), "run_id": run_id,
                    "cutoff_seq": max(r["cutoff_seq"] for r in runs)}
        raise FisheriesError("watermark_uncommitted", "no complete fisheries run is committed yet; partial runs are "
                                                      "never evaluated")

    def generation(self, namespace: str) -> int:
        from src.kb.fisheries_identity import FisheriesLinks

        accepted = 0
        if table_exists(self.conn, "fisheries_identity_matches"):
            accepted = int(self.conn.execute("SELECT count(*) FROM fisheries_identity_matches WHERE namespace=? AND "
                                             "state='accepted'", [namespace]).fetchone()[0])
        return FisheriesLinks(self.conn, initialize=False).generation(namespace) + accepted

    # ------------------------------------------------------------------ snapshot

    @staticmethod
    def _side(record: Mapping[str, Any], revision: Mapping[str, Any] | None) -> dict[str, Any]:
        if revision is None:
            return {"state": "none on record", "date": None, "citation": None}
        source = revision["statement"]["source"]
        published = revision["statement"]["as_published"]
        return {"state": revision["event"], "date": revision["effective_from"] or revision["snapshot_date"],
                "as_published": published,
                "citation": {"record_id": record["record_id"], "revision_id": revision["revision_id"],
                             "provider": record["provider"], "url": source["url"], "locator": source["locator"],
                             "snapshot_date": revision["snapshot_date"],
                             "release": source.get("release") or source.get("dataset_version"),
                             "retrieved_at_ms": revision["observed_at_ms"]}}

    def _watched(self, namespace: str, watch: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        chosen: dict[str, tuple[str, dict[str, Any]]] = {}
        member_of = {}
        for vessel in watch.get("vessels") or []:
            for member in self.identity.members(namespace, vessel):
                member_of[member] = vessel
        for record in self.store.records(namespace):
            if record["record_type"] in {"authorisation", "listing"}:
                if record["subject_key"] in member_of:
                    chosen.setdefault(record["record_id"], (f"vessel:{member_of[record['subject_key']]}", record))
                elif record["list_key"] in set(watch.get("lists") or []):
                    chosen.setdefault(record["record_id"], (f"list:{record['list_key']}", record))
                continue
            if record["record_type"] not in {"catch_observation", "effort_aggregate"}:
                continue
            if record["list_key"] in set(watch.get("lists") or []):
                chosen.setdefault(record["record_id"], (f"list:{record['list_key']}", record))
                continue
            published = self.store.revisions(namespace, record["record_id"])[-1]["statement"]["as_published"]
            if str(published["area"]["code"]) in set(watch.get("areas") or []):
                chosen.setdefault(record["record_id"], (f"area:{published['area']['code']}", record))
            elif published.get("species") in set(watch.get("species") or []):
                chosen.setdefault(record["record_id"], (f"species:{published['species']}", record))
        return [chosen[k] for k in sorted(chosen)]

    def snapshot(self, subscription: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
        namespace = subscription["namespace"]
        items = []
        for watched, record in self._watched(namespace, subscription["query"]["watch"]):
            revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff)
            for index, revision in enumerate(revisions):
                prior = revisions[index - 1] if index else None
                items.append({"id": f"{watched}:{record['record_id']}:{revision['revision_id']}",
                              "kind": _kind(record["record_type"], revision["event"], index), "watched": watched,
                              "subject_key": record["subject_key"], "record_key": record["record_key"],
                              "prior": self._side(record, prior), "new": self._side(record, revision)})
        return {"items": items, "coverage": {"complete": True}}

    # ------------------------------------------------------------------ run

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        mark = self.complete_watermark(namespace, watermark)
        generation = self.generation(namespace)
        if generation >= GENERATION_SPAN:
            raise FisheriesError("generation_overflow", "link and identity generation exceeds the watermark span")
        combined = mark["source_watermark"] * GENERATION_SPAN + generation
        self.subscriptions.commit_watermark(
            namespace, combined, kind="ingestion",
            detail={"source_pack": SOURCE_PACK, "source_watermark": mark["source_watermark"],
                    "link_identity_generation": generation, "cutoff_seq": mark["cutoff_seq"]},
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
            notifications.append({"contract": CONTRACT, "notification_id": f"{event_id}:{item['kind']}",
                                  "event_id": event_id, "kind": item["kind"], "object": key,
                                  "watched": item["watched"], "subject_key": item["subject_key"],
                                  "record_key": item["record_key"], "prior": item["prior"], "new": item["new"],
                                  "baseline": baseline, "message": _message(item)})
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": combined,
                "source_watermark": mark["source_watermark"], "run_id": mark["run_id"], "baseline": baseline,
                "notifications": notifications, "delivery": subscription["delivery"]}

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        if not table_exists(self.conn, "knowledge_subscriptions"):
            raise FisheriesError("not_ready", "no fisheries monitor exists yet")
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)


def _message(item: Mapping[str, Any]) -> str:
    """A plain restatement of what the source published; never advice or an enforcement suggestion."""
    new, prior = item["new"], item["prior"]
    what = item["kind"].replace("_", " ")
    when = f" dated {new['date']}" if new.get("date") else ""
    release = (new.get("citation") or {}).get("release")
    before = f"; previously {prior['state']}" + (f" ({prior['date']})" if prior.get("date") else "")
    return f"{item['record_key']}: {what}{when}" + (f" in release {release}" if release else "") + before + "."


__all__ = ["CONTRACT", "EVENT_KINDS", "FisheriesMonitor"]
