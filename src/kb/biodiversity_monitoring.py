"""Monitor new occurrences, checklist releases and assessment changes through ``platform.subscriptions`` (BD10 #2528).

A biodiversity monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), as the Climate and
Environment monitors are (:mod:`src.kb.environment_monitoring`): no monitor
table, queue or scheduler of its own. The query names what is watched -
**taxa** (resolved at each evaluation through accepted identity matches only),
**places** (occurrences linked within, uncertain or by code) and **datasets**
(publishing dataset keys). The ``climate-environment-biodiversity`` source
pack's schedule refreshes the bounded selections under their budgets, with
receipts, respecting the GBIF and IUCN limits recorded in BD01.

Every stored revision is one cumulative item; the subscription store turns new
items into delivered events, so unchanged data emits nothing and a replay at
the same watermark delivers nothing:

* ``occurrence_new`` / ``occurrence_revised`` / ``occurrence_removed`` (tombstone);
* ``taxonomic_status_change`` - a dated change between two checklist releases;
* ``assessment_new`` / ``assessment_revised`` (e.g. the assessor's ``latest``
  designation moved to a newer assessment).

Each event cites the prior and the new revision. A monitor evaluates only at a
committed source-pack watermark whose run completed every biodiversity source
it ran.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.biodiversity_records import (
    NOTIFICATION_CONTRACT,
    READ_SCOPE,
    SOURCE_PACK,
    BiodiversityError,
    authorize,
)
from src.kb.biodiversity_store import BiodiversityStore, table_exists

KIND = "biodiversity-monitor"
EVENT_KINDS = ("occurrence_new", "occurrence_revised", "occurrence_removed", "taxonomic_status_change",
               "assessment_new", "assessment_revised")
GENERATION_SPAN = 1_000_000_000


class BiodiversityMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.biodiversity_identity import BiodiversityIdentity
        from src.kb.biodiversity_links import BiodiversityLinks
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = BiodiversityStore(conn, initialize=initialize, now=self.now)
        self.identity = BiodiversityIdentity(conn, initialize=initialize, now=self.now)
        self.links = BiodiversityLinks(conn, initialize=initialize, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, principal_id: str, scopes: Iterable[str],
               taxa: list[str] | None = None, places: list[str] | None = None, datasets: list[str] | None = None,
               delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        """Watch taxa (name, native key or provider:key), place ids or publishing dataset keys."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        if not (taxa or places or datasets):
            raise BiodiversityError("invalid_watch", "watch at least one taxon, place or dataset")
        for taxon in taxa or []:
            if not self.identity.find(namespace, taxon)[1]:
                raise BiodiversityError("not_found", f"{taxon!r} reaches no acquired taxon")
        from src.kb.biodiversity_links import place_view

        for place_id in places or []:
            if place_view(self.conn, namespace, place_id) is None:
                raise BiodiversityError("not_found", f"place {place_id!r} is not visible")
        watch = {"taxa": sorted(set(taxa or [])), "places": sorted(set(places or [])),
                 "datasets": sorted(set(datasets or []))}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "environment",
             "query": {"operation": "search", "kind": KIND, "watch": watch},
             "filters": {"watch": ["taxa", "places", "datasets"]},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "biodiversity-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": f"evaluated at complete {SOURCE_PACK} source-pack runs within the declared "
                                      "bounds and provider limits; no separate scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != KIND:
            raise BiodiversityError("monitor_not_found", "subscription is not a biodiversity monitor")
        return subscription

    def complete_watermark(self, namespace: str, watermark: int | None = None) -> dict[str, Any]:
        if not table_exists(self.conn, "source_pack_watermarks") or not table_exists(self.conn,
                                                                                      "biodiversity_source_runs"):
            raise BiodiversityError("watermark_uncommitted", "no committed biodiversity run yet")
        rows = self.conn.execute(
            "SELECT watermark, committed_at_ms, run_id FROM source_pack_watermarks WHERE pack_id=? "
            "AND (? IS NULL OR watermark=?) ORDER BY watermark DESC", [SOURCE_PACK, watermark, watermark]).fetchall()
        for mark, committed_at, run_id in rows:
            runs = self.store.runs(namespace, run_id)
            if not runs:
                continue
            if any(r["status"] != "complete" for r in runs):
                if watermark is not None:
                    raise BiodiversityError("incomplete_run", "that run did not complete every source it ran")
                continue
            return {"source_watermark": int(mark), "committed_at_ms": int(committed_at), "run_id": run_id,
                    "cutoff_seq": max(r["cutoff_seq"] for r in runs)}
        raise BiodiversityError("watermark_uncommitted", "no complete biodiversity run is committed yet")

    def generation(self, namespace: str) -> int:
        accepted = 0
        if table_exists(self.conn, "biodiversity_identity_matches"):
            accepted = int(self.conn.execute("SELECT count(*) FROM biodiversity_identity_matches WHERE namespace=? "
                                             "AND state IN ('accepted', 'reverted', 'rejected')",
                                             [namespace]).fetchone()[0])
        return self.links.generation(namespace) + accepted

    # ------------------------------------------------------------------ snapshot

    @staticmethod
    def _cite(record: Mapping[str, Any], revision: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if revision is None:
            return None
        return {"record_id": record["record_id"], "revision_id": revision["revision_id"],
                "event": revision["event"], "release": revision["release"], "retrieved_on": revision["retrieved_on"],
                "url": revision["statement"]["source"]["url"]}

    def _members(self, namespace: str, taxon: str) -> set[str]:
        return {m for s in self.identity.find(namespace, taxon)[1] for m in self.identity.members(namespace, s)}

    def snapshot(self, subscription: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
        from src.kb.biodiversity_links import classify, place_view

        namespace = subscription["namespace"]
        watch = subscription["query"]["watch"]
        taxa = {taxon: self._members(namespace, taxon) for taxon in watch.get("taxa") or []}
        places = [p for p in (place_view(self.conn, namespace, pid) for pid in watch.get("places") or []) if p]
        items = []

        def add(watched, kind, record, prior, new, **extra):
            items.append({"id": f"{watched}:{kind}:{new['revision_id'] if 'revision_id' in new else new['id']}",
                          "kind": kind, "watched": watched, "record_key": record["record_key"],
                          "prior": prior, "new": new, **extra})

        for record in self.store.records(namespace):
            revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff)
            if not revisions:
                continue
            if record["record_type"] == "occurrence":
                for index, revision in enumerate(revisions):
                    published = revision["statement"]["as_published"]
                    watched = [f"taxon:{t}" for t, members in taxa.items()
                               if f"gbif:{published.get('taxon_key')}" in members]
                    watched += [f"place:{p['place_id']}" for p in places
                                if classify(published, p)["relation"] in {"within", "uncertain", "code"}]
                    watched += [f"dataset:{d}" for d in watch.get("datasets") or [] if published["dataset_key"] == d]
                    kind = ("occurrence_removed" if revision["event"] == "removed" else
                            "occurrence_new" if index == 0 else "occurrence_revised")
                    for target in watched:
                        add(target, kind, record, self._cite(record, revisions[index - 1] if index else None),
                            self._cite(record, revision))
            elif record["record_type"] == "conservation_assessment":
                for index, revision in enumerate(revisions):
                    published = revision["statement"]["as_published"]
                    for taxon, members in taxa.items():
                        if f"iucn:{published['taxon_id']}" in members:
                            add(f"taxon:{taxon}", "assessment_revised" if index else "assessment_new", record,
                                self._cite(record, revisions[index - 1] if index else None),
                                self._cite(record, revision), category=published["category"],
                                year_published=published["year_published"], latest=published["latest"],
                                scope=published["scope"]["label"])
        for taxon, members in taxa.items():
            for change in self.store.status_changes(namespace, cutoff_seq=cutoff):
                if change["subject_key"] in members:
                    items.append({"id": f"taxon:{taxon}:taxonomic_status_change:{change['change_id']}",
                                  "kind": "taxonomic_status_change", "watched": f"taxon:{taxon}",
                                  "record_key": change["subject_key"], "prior": change["from"], "new": change["to"],
                                  "changed_on": change["changed_on"]})
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
            notifications.append({"contract": NOTIFICATION_CONTRACT, "notification_id": f"{event_id}:{item['kind']}",
                                  "event_id": event_id, "kind": item["kind"], "object": key,
                                  "watched": item["watched"], "record_key": item["record_key"],
                                  "prior": item["prior"], "new": item["new"], "baseline": baseline})
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": combined,
                "source_watermark": mark["source_watermark"], "run_id": mark["run_id"], "baseline": baseline,
                "notifications": notifications, "delivery": subscription["delivery"]}

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        if not table_exists(self.conn, "knowledge_subscriptions"):
            raise BiodiversityError("not_ready", "no biodiversity monitor exists yet")
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)


__all__ = ["EVENT_KINDS", "KIND", "BiodiversityMonitor"]
