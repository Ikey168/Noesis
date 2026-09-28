"""Monitor new directives, revisions, reports and recommendation status changes (ES14, #2075).

A monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`)
whose query names what is watched: subjects (the same equivalence as
lookups, :func:`src.kb.engineering_safety_identity.query_keys`), issuing
authorities, directive numbers and recommendation numbers. There is no
monitor table or scheduler of its own; the ``engineering-safety`` source
pack's schedule refreshes the sources.

Each evaluation lists cumulative items, one per event, each citing the record
revision and, for a change, the previous revision:

* ``new_directive``, ``new_investigation``, ``new_recommendation``,
  ``new_defect_investigation``, ``new_occurrence`` - the first revision of a
  watched record;
* ``directive_revised`` and ``directive_supersedes`` (a revision stating that
  it supersedes or revises another directive; watchers of the superseded
  number get ``directive_superseded``);
* ``report_final`` (a final report after a preliminary one) and
  ``finding_changed`` (new or changed findings or probable cause);
* ``recommendation_status_changed`` - each newly published dated status;
* ``defect_investigation_upgraded`` - an ODI action stating an upgrade.

Only revisions that became current when they arrived count, so a late older
statement kept as history is never an event. Watermarks: a monitor evaluates
at the latest committed source-pack watermark whose run completed every
source it ran, with only the revisions projected up to that run; the
subscription watermark combines it with the review generation so a review
between runs is evaluated too, and replaying either adds nothing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.engineering_safety_identity import SubjectIdentity, key_matches, query_keys
from src.kb.engineering_safety_records import (
    AUTHORITIES,
    NOTIFICATION_CONTRACT,
    READ_SCOPE,
    SOURCE_PACK,
    EngineeringSafetyError,
    authorize,
)
from src.kb.engineering_safety_store import EngineeringSafetyStore, rank, table_exists

EVENT_KINDS = (
    "new_directive", "new_investigation", "new_recommendation", "new_defect_investigation", "new_occurrence",
    "new_complaint", "directive_revised", "directive_supersedes", "directive_superseded", "report_final",
    "finding_changed", "recommendation_status_changed", "defect_investigation_upgraded", "record_revised",
)
_NEW = {"directive": "new_directive", "investigation": "new_investigation",
        "safety_recommendation": "new_recommendation", "defect_investigation": "new_defect_investigation",
        "occurrence": "new_occurrence", "complaint": "new_complaint"}
GENERATION_SPAN = 1_000_000_000
AUTHORITY_CODES = sorted({code for _, code, _ in AUTHORITIES.values()})


class EngineeringSafetyMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = EngineeringSafetyStore(conn, initialize=False, now=self.now)
        self.identity = SubjectIdentity(conn, initialize=False, now=self.now)
        self.initialize = initialize
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    # ------------------------------------------------------------ create

    def _watch(self, namespace: str, watch: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"subjects", "authorities", "directives", "recommendations"}
        watch = dict(watch or {})
        if set(watch) - allowed or not any(watch.get(k) for k in allowed):
            raise EngineeringSafetyError("invalid_watch", "watch subjects, authorities, directives or "
                                                          "recommendations (at least one)")
        subjects = [dict(s) for s in watch.get("subjects") or []]
        for spec in subjects:
            query_keys(self.identity, namespace, spec)  # the same validation as lookups
        authorities = sorted({str(a) for a in watch.get("authorities") or []})
        if set(authorities) - set(AUTHORITY_CODES):
            raise EngineeringSafetyError("invalid_watch", f"authorities are one of {AUTHORITY_CODES}")
        numbers = {}
        for key in ("directives", "recommendations"):
            values = sorted({str(v).strip() for v in watch.get(key) or [] if str(v).strip()})
            for value in values:
                provider, _, native = value.partition(":")
                if provider not in AUTHORITIES or not native:
                    raise EngineeringSafetyError("invalid_watch", f"{key} are named provider:number")
            numbers[key] = values
        return {"subjects": subjects, "authorities": authorities, **numbers}

    def create(self, namespace: str, request_key: str, *, watch: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        normalized = self._watch(namespace, watch)
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "engineering-safety",
             "query": {"operation": "search", "kind": "engineering-safety-monitor", "watch": normalized},
             "filters": {"watch": sorted(k for k, v in normalized.items() if v)},
             "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK},
             "delivery": delivery or {"kind": "poll"}},
            "engineering-safety-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": f"evaluated at complete {SOURCE_PACK} source-pack runs; no separate scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "engineering-safety-monitor":
            raise EngineeringSafetyError("monitor_not_found", "subscription is not an engineering-safety monitor")
        return subscription

    # ------------------------------------------------------------ watermark

    def complete_watermark(self, namespace: str, watermark: int | None = None) -> dict[str, Any]:
        if not table_exists(self.conn, "source_pack_watermarks") or not table_exists(self.conn, "es_source_runs"):
            raise EngineeringSafetyError("watermark_uncommitted", "no committed engineering-safety run yet")
        rows = self.conn.execute(
            "SELECT watermark, committed_at_ms, run_id FROM source_pack_watermarks WHERE pack_id=? "
            "AND (? IS NULL OR watermark=?) ORDER BY watermark DESC", [SOURCE_PACK, watermark, watermark]).fetchall()
        for mark, committed_at, run_id in rows:
            runs = self.conn.execute("SELECT source_id, status, cutoff_seq FROM es_source_runs WHERE namespace=? AND "
                                     "run_id=?", [namespace, run_id]).fetchall()
            if not runs:
                continue
            if any(status != "complete" for _, status, _ in runs):
                if watermark is not None:
                    raise EngineeringSafetyError("incomplete_run", "that run did not complete every source it ran; "
                                                 "it is never evaluated",
                                                 failed=sorted(s for s, status, _ in runs if status != "complete"))
                continue
            return {"source_watermark": int(mark), "committed_at_ms": int(committed_at), "run_id": run_id,
                    "cutoff_seq": max(int(c) for _, _, c in runs)}
        raise EngineeringSafetyError("watermark_uncommitted", "no complete engineering-safety run is committed yet; "
                                                              "partial runs are never evaluated")

    # ------------------------------------------------------------ snapshot

    def _chain(self, namespace: str, record_id: str, cutoff: int) -> list[dict[str, Any]]:
        chain, best = [], None
        for revision in self.store.revisions(namespace, record_id):
            if revision["seq"] > cutoff:
                continue
            if best is None or rank(revision) >= rank(best):
                chain.append(revision)
                best = revision
        return chain

    def snapshot(self, subscription: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
        namespace, watch = subscription["namespace"], subscription["query"]["watch"]
        patterns = []
        for spec in watch["subjects"]:
            try:
                patterns += query_keys(self.identity, namespace, spec)[0]
            except EngineeringSafetyError:
                continue
        directives = set(watch.get("directives") or [])
        recommendations = set(watch.get("recommendations") or [])
        authorities = set(watch.get("authorities") or [])
        items: list[dict[str, Any]] = []
        for head in self.store.records(namespace):
            chain = self._chain(namespace, head["record_id"], cutoff)
            if not chain:
                continue
            number = f"{head['provider']}:{head['native_id']}"
            parts = [self.store.parts(namespace, r["revision_id"]) for r in chain]
            labels = []
            if head["authority"] in authorities:
                labels.append(f"authority:{head['authority']}")
            if number in directives and head["record_kind"] == "directive":
                labels.append(f"directive:{number}")
            if number in recommendations and head["record_kind"] == "safety_recommendation":
                labels.append(f"recommendation:{number}")
            if patterns and any(key_matches(s.get("subject_key"), patterns) for p in parts for s in p["subjects"]):
                labels.append("subject")
            for index, revision in enumerate(chain):
                for relation in parts[index]["relations"]:
                    target = f"{relation['target_provider']}:{relation['target_native_id']}"
                    if relation["relation"] in {"supersedes", "revises"} and target in directives:
                        items.append(self._item(head, revision, chain, index, "directive_superseded",
                                                f"directive:{target}", superseded=target))
            if not labels:
                continue
            label = labels[0]
            for index, revision in enumerate(chain):
                items += self._events(head, chain, parts, index, label)
        return {"items": items, "coverage": {"complete": True}}

    def _item(self, head, revision, chain, index, kind, label, **extra) -> dict[str, Any]:
        return {"id": f"{label}:{kind}:{revision['revision_id']}" + (f":{extra.get('status_key')}"
                                                                     if extra.get("status_key") else ""),
                "kind": kind, "watched": label, "record_id": head["record_id"], "provider": head["provider"],
                "native_id": head["native_id"], "record_kind": head["record_kind"], "authority": head["authority"],
                "revision_id": revision["revision_id"], "revision_no": revision["revision_no"],
                "revision_date": revision.get("revision_date"),
                **({"previous_revision_id": chain[index - 1]["revision_id"]} if index > 0 else {}),
                **{k: v for k, v in extra.items() if k != "status_key"}}

    def _events(self, head, chain, parts, index, label) -> list[dict[str, Any]]:
        revision, kind = chain[index], head["record_kind"]
        events = []
        if index == 0:
            events.append(self._item(head, revision, chain, index, _NEW[kind], label))
        previous = parts[index - 1] if index else None
        current = parts[index]

        def texts(p, kinds):
            return sorted(s["text"] for s in (p or {}).get("statements", []) if s["kind"] in kinds)

        def relations(p, names):
            return {(r["relation"], r["target_provider"], r["target_native_id"]) for r in (p or {}).get("relations", [])
                    if r["relation"] in names}

        if index > 0:
            if kind == "directive":
                events.append(self._item(head, revision, chain, index, "directive_revised", label,
                                         revision_label=revision.get("revision_label")))
            elif kind == "investigation" and revision.get("report_status") == "final" and \
                    chain[index - 1].get("report_status") != "final":
                events.append(self._item(head, revision, chain, index, "report_final", label))
            elif kind not in {"safety_recommendation", "investigation", "defect_investigation"}:
                events.append(self._item(head, revision, chain, index, "record_revised", label))
        if kind == "directive":
            for relation, provider, native in sorted(relations(current, {"supersedes", "revises"}) -
                                                     relations(previous, {"supersedes", "revises"})):
                events.append(self._item(head, revision, chain, index, "directive_supersedes", label,
                                         superseded=f"{provider}:{native}", relation=relation))
        if kind == "investigation" and texts(current, {"finding", "probable_cause", "root_cause"}) != texts(
                previous, {"finding", "probable_cause", "root_cause"}) and texts(
                current, {"finding", "probable_cause", "root_cause"}):
            events.append(self._item(head, revision, chain, index, "finding_changed", label))
        if kind == "safety_recommendation":
            before = {(r["status"], r.get("status_date")) for r in (previous or {}).get("responses", [])}
            for response in current["responses"]:
                key = (response["status"], response.get("status_date"))
                if key in before:
                    continue
                if index > 0:  # the first revision's statuses are the baseline of new_recommendation
                    events.append(self._item(head, revision, chain, index, "recommendation_status_changed", label,
                                             status=response["status"], status_date=response.get("status_date"),
                                             status_key=f"{response['status']}@{response.get('status_date')}"))
        if kind == "defect_investigation":
            for relation, _, native in sorted(relations(current, {"upgraded_to", "upgraded_from"}) -
                                              relations(previous, {"upgraded_to", "upgraded_from"})):
                events.append(self._item(head, revision, chain, index, "defect_investigation_upgraded", label,
                                         relation=relation, other_action=native))
        return events

    # ------------------------------------------------------------ run and poll

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        self.store.require_ready()
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        mark = self.complete_watermark(namespace, watermark)
        generation = self.store.generation(namespace)
        if generation >= GENERATION_SPAN:
            raise EngineeringSafetyError("generation_overflow", "review generation exceeds the watermark span")
        combined = mark["source_watermark"] * GENERATION_SPAN + generation
        self.subscriptions.commit_watermark(
            namespace, combined, kind="ingestion",
            detail={"source_pack": SOURCE_PACK, "source_watermark": mark["source_watermark"],
                    "review_generation": generation, "record_cutoff_seq": mark["cutoff_seq"]},
            committed_at_ms=mark["committed_at_ms"])
        evaluated = self.subscriptions.evaluate(subscription_id, combined,
                                                self.snapshot(subscription, mark["cutoff_seq"]),
                                                principal_id=principal_id, scopes=scopes,
                                                observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            event_type, key, after = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id]).fetchone()
            if event_type != "added" or not after:
                continue  # items are cumulative per revision; only additions are news
            item = json.loads(after)
            notifications.append({
                "contract": NOTIFICATION_CONTRACT, "notification_id": f"{event_id}:{item['kind']}",
                "event_id": event_id, "kind": item["kind"], "object": key, "message": _message(item),
                "cites": {k: item[k] for k in ("record_id", "provider", "native_id", "revision_id", "revision_no",
                                               "revision_date", "previous_revision_id") if item.get(k) is not None},
                **{k: item[k] for k in ("watched", "superseded", "relation", "status", "status_date",
                                        "other_action", "revision_label") if k in item},
            })
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": combined,
                "source_watermark": mark["source_watermark"], "review_generation": generation,
                "run_id": mark["run_id"], "baseline": subscription.get("last_watermark") is None,
                "notifications": notifications, "delivery": subscription["delivery"]}

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        if not self.store.ready() or not table_exists(self.conn, "knowledge_subscriptions"):
            raise EngineeringSafetyError("not_ready", "no engineering-safety monitor or source run exists yet")
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)


def _message(item: Mapping[str, Any]) -> str:
    name = f"{item['provider']} {item['native_id']}"
    kind = item["kind"]
    if kind.startswith("new_"):
        return f"{name} was published (revision {item['revision_no']})."
    if kind == "directive_supersedes":
        return f"{name} states that it {item.get('relation', 'supersedes')} {item.get('superseded')}."
    if kind == "directive_superseded":
        return f"{item.get('superseded')} is superseded or revised by {name}, as that directive states."
    if kind == "report_final":
        return f"The final report of {name} was published; the preliminary report is kept."
    if kind == "finding_changed":
        return f"{name} states new or changed findings or probable cause (quoted in the record)."
    if kind == "recommendation_status_changed":
        return f"{name} status as published: {item.get('status')} ({item.get('status_date') or 'undated'})."
    if kind == "defect_investigation_upgraded":
        return f"{name} states an upgrade relation with {item.get('other_action')}."
    return f"{name} has a new revision ({item.get('revision_date') or 'undated'})."


__all__ = ["EVENT_KINDS", "EngineeringSafetyMonitor"]
