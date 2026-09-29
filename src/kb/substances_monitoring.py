"""Monitor classification, SVHC and restriction changes through ``platform.subscriptions`` (#2212, CH10 #2309).

A substance monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`) whose query names the
watched substances (provider subject keys; the records an accepted identity
decision joins to them are watched too). There is no monitor table, queue or
scheduler of its own: the ``chemicals-substances`` source pack's schedule
refreshes the sources, and deliveries go through the subscription outbox.

Each evaluation lists cumulative items, one per event, and the subscription
store turns new items into delivered events:

* ``classification_revision`` - a harmonised classification revision (a new
  ATP) or a new notified aggregate;
* ``candidate_list_inclusion`` / ``_amendment`` / ``_removal`` - SVHC events;
* ``authorisation_inclusion`` / ``_amendment`` / ``_removal`` - Annex XIV;
* ``restriction_inclusion`` / ``_amendment`` / ``_removal`` - Annex XVII;
* ``linked_notice`` - a product safety notice newly linked by citation.

Every event shows the prior and the new status, each with its citation and
effective date, quoted from the source; no advisory text is added. A monitor
evaluates only at a committed source-pack watermark whose run completed every
substance source it ran, with only the revisions projected up to that run;
the subscription watermark also counts link and identity changes, so replaying
either adds nothing and duplicates are never delivered.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.substances_records import READ_SCOPE, SOURCE_PACK, SubstanceError, authorize
from src.kb.substances_store import SubstanceStore, table_exists

CONTRACT = "noesis-substance-notification-v1"
KIND = "substance-monitor"
WATCH_TYPES = {"classification": "classification_revision", "candidate_listing": "candidate_list",
               "authorisation": "authorisation", "restriction": "restriction"}
EVENT_KINDS = ("classification_revision", "candidate_list_inclusion", "candidate_list_amendment",
               "candidate_list_removal", "authorisation_inclusion", "authorisation_amendment",
               "authorisation_removal", "restriction_inclusion", "restriction_amendment", "restriction_removal",
               "linked_notice")
GENERATION_SPAN = 1_000_000_000


class SubstanceMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.subscriptions import SubscriptionStore
        from src.kb.substances_identity import SubstanceIdentity

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = SubstanceStore(conn, initialize=initialize, now=self.now)
        self.identity = SubstanceIdentity(conn, initialize=initialize, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    # ------------------------------------------------------------------ create

    def create(self, namespace: str, request_key: str, *, substances: list[str], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        """Watch substances named by subject key or by an unambiguous name, CAS, EC, InChIKey or DTXSID."""
        from src.kb.substances_queries import SubstanceQueries

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not substances:
            raise SubstanceError("invalid_watch", "watch at least one substance")
        queries = SubstanceQueries(self.conn)
        subjects = set()
        for entry in substances:
            known = {s["subject_key"] for s in self.store.subjects(namespace)}
            members, _ = (queries.members(namespace, scopes=scopes, subject_key=entry) if entry in known
                          else queries.members(namespace, scopes=scopes, query=entry))
            if not members:
                raise SubstanceError("not_found", f"{entry!r} reaches no acquired substance record")
            subjects.add(min(members))
        watch = {"subjects": sorted(subjects), "requested": sorted(set(substances))}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "chemicals",
             "query": {"operation": "search", "kind": KIND, "watch": watch},
             "filters": {"watch": ["subjects"]},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "substance-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": f"evaluated at complete {SOURCE_PACK} source-pack runs; no separate scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != KIND:
            raise SubstanceError("monitor_not_found", "subscription is not a substance monitor")
        return subscription

    # ------------------------------------------------------------------ watermark

    def complete_watermark(self, namespace: str, watermark: int | None = None) -> dict[str, Any]:
        if not table_exists(self.conn, "source_pack_watermarks") or not table_exists(self.conn,
                                                                                      "substance_source_runs"):
            raise SubstanceError("watermark_uncommitted", "no committed substance run yet")
        rows = self.conn.execute(
            "SELECT watermark, committed_at_ms, run_id FROM source_pack_watermarks WHERE pack_id=? "
            "AND (? IS NULL OR watermark=?) ORDER BY watermark DESC", [SOURCE_PACK, watermark, watermark]).fetchall()
        for mark, committed_at, run_id in rows:
            runs = [r for r in self.store.runs(namespace, run_id)]
            if not runs:
                continue
            if any(r["status"] != "complete" for r in runs):
                if watermark is not None:
                    raise SubstanceError("incomplete_run", "that run did not complete every substance source; it is "
                                                           "never evaluated",
                                         failed=sorted(r["source_id"] for r in runs if r["status"] != "complete"))
                continue
            return {"source_watermark": int(mark), "committed_at_ms": int(committed_at), "run_id": run_id,
                    "cutoff_seq": max(r["cutoff_seq"] for r in runs)}
        raise SubstanceError("watermark_uncommitted", "no complete substance run is committed yet; partial runs are "
                                                      "never evaluated")

    def generation(self, namespace: str) -> int:
        from src.kb.substances_links import SubstanceLinks

        links = SubstanceLinks(self.conn, initialize=False).generation(namespace)
        return links + len(self.identity.accepted(namespace))

    # ------------------------------------------------------------------ snapshot

    @staticmethod
    def _side(record: Mapping[str, Any], revision: Mapping[str, Any] | None, *, none_state: str) -> dict[str, Any]:
        if revision is None:
            return {"state": none_state, "effective_from": None, "citation": None}
        source = revision["statement"]["source"]
        return {"state": revision["event"], "effective_from": revision["effective_from"],
                "as_published": revision["statement"]["as_published"],
                "citation": {"record_id": record["record_id"], "revision_id": revision["revision_id"],
                             "provider": record["provider"], "url": source["url"], "locator": source["locator"],
                             "legal_act": revision["legal_act"]}}

    def snapshot(self, subscription: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
        from src.kb.substances_links import SubstanceLinks

        namespace = subscription["namespace"]
        items: list[dict[str, Any]] = []
        for watched in subscription["query"]["watch"]["subjects"]:
            members = self.identity.members(namespace, watched)
            for record_type, label in WATCH_TYPES.items():
                for record in self.store.records(namespace, subject_keys=members, record_type=record_type):
                    revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff)
                    ordered = sorted(revisions, key=lambda r: (r["effective_from"] or "", r["seq"]))
                    for index, revision in enumerate(ordered):
                        prior = ordered[index - 1] if index else None
                        kind = label if record_type == "classification" else f"{label}_{revision['event']}"
                        items.append({
                            "id": f"{watched}:{record['record_id']}:{revision['revision_id']}", "kind": kind,
                            "watched": watched, "subject_key": record["subject_key"],
                            "record_key": record["record_key"],
                            "prior": self._side(record, prior, none_state="none on record"),
                            "new": self._side(record, revision, none_state="none on record")})
            links = SubstanceLinks(self.conn, initialize=False)
            for link in links.links(namespace, members, scopes={"operator"}, owner="products"):
                items.append({"id": f"{watched}:{link['link_id']}", "kind": "linked_notice", "watched": watched,
                              "subject_key": link["subject_key"], "notice_id": link["target_id"],
                              "prior": {"state": "not linked", "effective_from": None, "citation": None},
                              "new": {"state": "linked by citation", "effective_from": None,
                                      "citation": {"link_id": link["link_id"], "basis": link["basis"],
                                                   "matched": link["matched"], "citing_text": link["citing_text"],
                                                   "notice_revision": link["target_revision"]}}})
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
            raise SubstanceError("generation_overflow", "link and identity generation exceeds the watermark span")
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
                continue  # items are cumulative per revision and link; only additions are news
            item = json.loads(after)
            notifications.append({"contract": CONTRACT, "notification_id": f"{event_id}:{item['kind']}",
                                  "event_id": event_id, "kind": item["kind"], "object": key,
                                  "watched": item["watched"], "subject_key": item["subject_key"],
                                  "prior": item["prior"], "new": item["new"], "baseline": baseline,
                                  "message": _message(item)})
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": combined,
                "source_watermark": mark["source_watermark"], "run_id": mark["run_id"], "baseline": baseline,
                "notifications": notifications, "delivery": subscription["delivery"]}

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        if not table_exists(self.conn, "knowledge_subscriptions"):
            raise SubstanceError("not_ready", "no substance monitor exists yet")
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)


def _message(item: Mapping[str, Any]) -> str:
    """A plain restatement of what the source published; never advice."""
    new, prior = item["new"], item["prior"]
    act = ((new.get("citation") or {}).get("legal_act") or {}).get("title")
    if item["kind"] == "linked_notice":
        return f"A product safety notice now cites this substance ({new['citation']['matched']})."
    what = item["kind"].replace("_", " ")
    since = f" effective {new['effective_from']}" if new.get("effective_from") else ""
    before = f"; previously {prior['state']}" + (f" (effective {prior['effective_from']})"
                                                 if prior.get("effective_from") else "")
    return f"{item['record_key']}: {what}{since}" + (f" under {act}" if act else "") + before + "."


__all__ = ["CONTRACT", "EVENT_KINDS", "SubstanceMonitor"]
