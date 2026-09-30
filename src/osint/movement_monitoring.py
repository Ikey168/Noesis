"""Monitor registry revisions, identity decisions and listing changes through ``platform.subscriptions`` (#2282, MV12).

A movement monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`) whose query names at most 25
aircraft or vessel identifiers (the MV01 bound) and what is watched about them:

* ``registry`` - registry revisions: ``registry_published``, ``registry_amended``,
  ``registry_deregistered`` (a deregistration is a revision, never a deletion);
* ``identity`` - reviewed identifier matches: ``identity_match_accepted``,
  ``identity_match_rejected``, ``identity_match_reverted``;
* ``listing`` - sanctions listing revisions of the linked designations:
  ``listing_listed``, ``listing_amended``, ``listing_delisted`` (as published,
  cited to the list revision and snapshot; never a screening verdict).

Position and call subscriptions are refused: live movement alerts are out of
scope. There is no monitor table, queue or scheduler of its own: the
``bounded-public-osint`` source pack refreshes the movement sources within the
MV01 bounds, with receipts, and a monitor evaluates only at a committed
source-pack watermark whose movement sources all completed. Every event cites
the prior and the new revision; replays deliver nothing twice.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.osint.movements import (
    BOUNDS,
    IDENTITY_PREFIX,
    KEY_SCHEMES,
    READ_SCOPE,
    SOURCE_PACK,
    MovementError,
    MovementIdentity,
    MovementLinks,
    MovementStore,
    classify,
    identifier_key,
    require,
    subject_key,
    table_exists,
)

CONTRACT = "noesis-osint-movement-notification-v1"
KIND = "osint-movement-monitor"
WATCHES = ("registry", "identity", "listing")
REFUSED_WATCHES = frozenset({"position", "positions", "sample", "samples", "call", "calls", "movement", "movements",
                             "track", "tracks", "location", "locations"})
EVENT_KINDS = ("registry_published", "registry_amended", "registry_deregistered", "identity_match_accepted",
               "identity_match_rejected", "identity_match_reverted", "listing_listed", "listing_amended",
               "listing_delisted")
GENERATION_SPAN = 1_000_000_000


class MovementMonitor:
    def __init__(self, conn: Any, *, now=None) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = MovementStore(conn, initialize=False)
        self.identity = MovementIdentity(conn)
        self.subscriptions = SubscriptionStore(conn)

    # ------------------------------------------------------------------ create

    def create(self, namespace: str, request_key: str, *, principal_id: str, scopes: Iterable[str],
               identifiers: list[str], watch: Iterable[str] = WATCHES, sanctions_namespace: str | None = None,
               delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        """Watch registry revisions, identity decisions and listing changes of named aircraft or vessels."""
        scopes = set(scopes)
        require(scopes, READ_SCOPE)
        watch = [str(w).casefold() for w in watch]
        if REFUSED_WATCHES & set(watch):
            raise MovementError("movement_subscription_refused", "position and call subscriptions are refused: live "
                                                                 "movement alerts are out of scope; monitors watch "
                                                                 f"{list(WATCHES)} only")
        unknown = sorted(set(watch) - set(WATCHES))
        if unknown or not watch:
            raise MovementError("invalid_watch", f"watch one or more of {list(WATCHES)}")
        limit = BOUNDS["monitor"]["max_identifiers"]
        if not identifiers or len(identifiers) > limit:
            raise MovementError("over_bound", f"a monitor watches 1..{limit} identifiers (MV01 bound)")
        subjects = []
        for identifier in identifiers:
            scheme, key = classify(identifier)
            if self.store.refusal(namespace, scheme, key):
                raise MovementError("privacy_opt_out", f"{scheme} {key} is on the privacy refusal list")
            subjects.append(subject_key(scheme, key))
        if "listing" in watch and not sanctions_namespace:
            raise MovementError("invalid_watch", "a listing watch names the sanctions namespace to read")
        query = {"operation": "search", "kind": KIND,
                 "watch": {"subjects": sorted(set(subjects)), "watch": sorted(set(watch)),
                           "sanctions_namespace": sanctions_namespace,
                           "requested": sorted(set(str(i) for i in identifiers))}}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "osint", "query": query, "filters": {"watch": sorted(set(watch))},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "osint-movement-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": f"evaluated at complete {SOURCE_PACK} source-pack runs of the movement sources "
                                      "within the MV01 bounds; no separate scheduler and no movement alerts"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != KIND:
            raise MovementError("monitor_not_found", "subscription is not a movement monitor")
        return subscription

    # ------------------------------------------------------------------ watermark

    def complete_watermark(self, namespace: str, watermark: int | None = None) -> dict[str, Any]:
        if not table_exists(self.conn, "source_pack_watermarks") or not table_exists(self.conn,
                                                                                      "osint_movement_source_runs"):
            raise MovementError("watermark_uncommitted", "no committed movement run yet")
        rows = self.conn.execute(
            "SELECT watermark, committed_at_ms, run_id FROM source_pack_watermarks WHERE pack_id=? AND (? IS NULL OR "
            "watermark=?) ORDER BY watermark DESC", [SOURCE_PACK, watermark, watermark]).fetchall()
        for mark, committed_at, run_id in rows:
            runs = self.store.runs(namespace, run_id)
            if not runs:
                continue
            if any(r["status"] != "complete" for r in runs):
                if watermark is not None:
                    raise MovementError("incomplete_run", "that run did not complete every movement source it ran; "
                                                          "it is never evaluated")
                continue
            return {"source_watermark": int(mark), "committed_at_ms": int(committed_at), "run_id": run_id,
                    "cutoff_seq": max(r["cutoff_seq"] for r in runs)}
        raise MovementError("watermark_uncommitted", "no complete movement run is committed yet; partial runs are "
                                                     "never evaluated")

    def generation(self, namespace: str, sanctions_namespace: str | None) -> int:
        count = 0
        if table_exists(self.conn, "ownership_identity_candidates"):
            count += int(self.conn.execute(
                "SELECT count(*) FROM ownership_identity_candidates WHERE namespace=? AND state<>'proposed' AND "
                "(left_key LIKE 'movements:%' OR right_key LIKE 'movements:%')", [namespace]).fetchone()[0])
        if sanctions_namespace and table_exists(self.conn, "sanctions_revisions"):
            count += int(self.conn.execute("SELECT count(*) FROM sanctions_revisions WHERE namespace=?",
                                           [sanctions_namespace]).fetchone()[0])
        if table_exists(self.conn, "osint_movement_links"):
            count += int(self.conn.execute("SELECT count(*) FROM osint_movement_links WHERE namespace=?",
                                           [namespace]).fetchone()[0])
        return count

    # ------------------------------------------------------------------ snapshot

    def _subjects(self, namespace: str, subject: str) -> set[str]:
        return set(self.identity.connected(namespace, subject)["subjects"])

    def _registry_items(self, namespace: str, watched: str, subjects: set[str], cutoff: int) -> list[dict[str, Any]]:
        items = []
        for record in self.store.records(namespace, record_type="registry_record"):
            revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff)
            if not revisions:
                continue
            stated = {subject_key(i["scheme"], i["value"]) for r in revisions for i in r["statement"]["identifiers"]
                      if i["scheme"] in KEY_SCHEMES and identifier_key(i["scheme"], i["value"])}
            if record["subject_key"] not in subjects and not stated & subjects:
                continue
            for index, revision in enumerate(revisions):
                kind = "registry_deregistered" if revision["event"] == "deregistered" else \
                    "registry_published" if index == 0 else "registry_amended"
                prior = revisions[index - 1] if index else None
                items.append({"id": f"registry:{watched}:{record['record_id']}:{revision['revision_id']}",
                              "kind": kind, "watched": watched, "record_key": revision["statement"]["record_key"],
                              "prior": _registry_side(record, prior), "new": _registry_side(record, revision)})
        return items

    def _identity_items(self, namespace: str, watched: str, subjects: set[str]) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return []
        keys = {IDENTITY_PREFIX + s for s in subjects}
        rows = self.conn.execute(
            "SELECT candidate_id, left_key, right_key, basis, state, decision_id FROM ownership_identity_candidates "
            "WHERE namespace=? AND state<>'proposed' ORDER BY candidate_id", [namespace]).fetchall()
        return [{"id": f"identity:{watched}:{r[0]}:{r[4]}:{r[5]}", "kind": f"identity_match_{r[4]}", "watched": watched,
                 "record_key": r[0], "prior": None,
                 "new": {"state": r[4], "date": None, "citation": {"candidate_id": r[0], "decision_id": r[5],
                                                                   "left": r[1], "right": r[2], "basis": r[3]}}}
                for r in rows if {r[1], r[2]} & keys]

    def _listing_items(self, namespace: str, watched: str, subjects: set[str], sanctions_namespace: str,
                       scopes: set[str]) -> list[dict[str, Any]]:
        from src.kb.sanctions import SanctionsStore, authorize

        authorize(sanctions_namespace, scopes, "knowledge:sanctions:read")
        store = SanctionsStore(self.conn, initialize=False)
        items = []
        for link in MovementLinks(self.conn).links(namespace, subjects):
            if link["target"] != "sanctions":
                continue
            history = store.history(sanctions_namespace, link["target_id"])
            for index, revision in enumerate(history):
                prior = history[index - 1] if index else None
                items.append({"id": f"listing:{watched}:{link['target_id']}:{revision['revision_id']}",
                              "kind": {"delisted": "listing_delisted", "listed": "listing_listed"}.get(
                                  revision["change"], "listing_amended"),
                              "watched": watched, "record_key": link["evidence"].get("record_key"),
                              "prior": _listing_side(prior), "new": _listing_side(revision),
                              "link_id": link["link_id"]})
        return items

    def snapshot(self, subscription: Mapping[str, Any], cutoff: int, scopes: set[str]) -> dict[str, Any]:
        namespace = subscription["namespace"]
        query = subscription["query"]["watch"]
        items = []
        for watched in query["subjects"]:
            subjects = self._subjects(namespace, watched)
            if "registry" in query["watch"]:
                items += self._registry_items(namespace, watched, subjects, cutoff)
            if "identity" in query["watch"]:
                items += self._identity_items(namespace, watched, subjects)
            if "listing" in query["watch"]:
                items += self._listing_items(namespace, watched, subjects, query["sanctions_namespace"], scopes)
        return {"items": items, "coverage": {"complete": True}}

    # ------------------------------------------------------------------ run

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        require(scopes, READ_SCOPE)
        mark = self.complete_watermark(namespace, watermark)
        generation = self.generation(namespace, subscription["query"]["watch"].get("sanctions_namespace"))
        if generation >= GENERATION_SPAN:
            raise MovementError("generation_overflow", "identity and listing generation exceeds the watermark span")
        combined = mark["source_watermark"] * GENERATION_SPAN + generation
        self.subscriptions.commit_watermark(
            namespace, combined, kind="ingestion",
            detail={"source_pack": SOURCE_PACK, "source_watermark": mark["source_watermark"],
                    "identity_listing_generation": generation, "cutoff_seq": mark["cutoff_seq"]},
            committed_at_ms=mark["committed_at_ms"])
        baseline = subscription["last_watermark"] is None
        evaluated = self.subscriptions.evaluate(subscription_id, combined,
                                                self.snapshot(subscription, mark["cutoff_seq"], scopes),
                                                principal_id=principal_id, scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            event_type, key, after = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id]).fetchone()
            if event_type != "added" or not after:
                continue  # items are cumulative per revision or decision; only additions are news
            item = json.loads(after)
            notifications.append({"contract": CONTRACT, "notification_id": f"{event_id}:{item['kind']}",
                                  "event_id": event_id, "kind": item["kind"], "object": key,
                                  "watched": item["watched"], "record_key": item["record_key"],
                                  "prior": item["prior"], "new": item["new"], "baseline": baseline,
                                  "message": _message(item)})
        receipts = [{"source_id": r["source_id"], "status": r["status"], "outcomes": r["outcomes"]}
                    for r in self.store.runs(namespace, mark["run_id"])]
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": combined,
                "source_watermark": mark["source_watermark"], "run_id": mark["run_id"], "baseline": baseline,
                "notifications": notifications, "receipts": receipts, "delivery": subscription["delivery"]}

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        if not table_exists(self.conn, "knowledge_subscriptions"):
            raise MovementError("not_ready", "no movement monitor exists yet")
        self._subscription(subscription_id, principal_id, scopes)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)


def _registry_side(record: Mapping[str, Any], revision: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if revision is None:
        return None
    value = revision["statement"]
    return {"state": revision["event"], "date": revision["effective_from"],
            "status": value["as_published"].get("status"),
            "citation": {"record_id": record["record_id"], "revision_id": revision["revision_id"],
                         "provider": record["provider"], "url": value["source"].get("url"),
                         "retrieved_at_ms": revision["observed_at_ms"]}}


def _listing_side(revision: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if revision is None:
        return None
    return {"state": revision["change"], "date": (revision.get("source_dates") or {}).get("listed_on"),
            "citation": {"revision_id": revision["revision_id"], "list_id": revision["list_id"],
                         "source_revision": revision["source_revision"]}}


def _message(item: Mapping[str, Any]) -> str:
    """A plain restatement of what the source published; never advice, a verdict or a location."""
    new, prior = item["new"] or {}, item["prior"] or {}
    what = item["kind"].replace("_", " ")
    when = f" dated {new['date']}" if new.get("date") else ""
    before = f"; previously {prior['state']}" if prior.get("state") else ""
    return f"{item['record_key']}: {what}{when}{before}."


__all__ = ["CONTRACT", "EVENT_KINDS", "KIND", "MovementMonitor", "REFUSED_WATCHES", "WATCHES"]
